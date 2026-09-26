import gc
import json
import time
import board
import displayio
import busio
import rtc
from digitalio import DigitalInOut
import neopixel
import adafruit_requests
import adafruit_connection_manager
import adafruit_ntp
from adafruit_esp32spi import adafruit_esp32spi
import terminalio
from adafruit_display_text import label
from adafruit_bitmap_font import bitmap_font
import adafruit_touchscreen
from secrets import secrets

USAGE_URL = secrets.get("proxy_url", "").rstrip("/") + "/all-usage"

REFRESH_MIN  = 900
REFRESH_MAX  = 14400
REFRESH_STEPS = [900, 1800, 3600, 7200, 14400]
RATE_LIMIT_FALLBACK = 3600
STARTUP_GRACE = 300
STARTUP_READY_GRACE = 15
MANUAL_REFRESH_MIN = 900
STATE_FILE = "usage_state.json"
USAGE_RETRY_KEY = "usage_retry_at"

VIEW_CLAUDE = 0
VIEW_CODEX  = 1
CAROUSEL_SECS = 60

W, H = 320, 240
SCALE = 2
X0   = 16         # centered: (320-288)/2 = 16
COLS = 24         # 24 * 12 = 288px wide
CW   = 6 * SCALE  # character width px = 12
CH   = 10 * SCALE # character height px = 20

BLACK  = 0x000000
WHITE  = 0xFFFFFF
ORANGE = 0xCC6B3D
TEAL   = 0x10A37F
SUB    = 0x666688
DIM    = 0x333355
GREEN  = 0x22c55e
AMBER  = 0xf59e0b
RED    = 0xef4444

FONT = None

def _init_font():
    global FONT
    if FONT is None:
        FONT = bitmap_font.load_font("/fonts/usage-ui.bdf")


def bar_color(pct):
    if pct is None: return DIM
    if pct < 50: return GREEN
    if pct < 80: return AMBER
    return RED


def _clamp_wait(seconds):
    seconds = int(seconds or 0)
    if seconds < REFRESH_MIN:
        seconds = RATE_LIMIT_FALLBACK
    if seconds > REFRESH_MAX:
        seconds = REFRESH_MAX
    return seconds


# ── hardware ──────────────────────────────────────────────────────────────────

def _init_esp():
    cs  = DigitalInOut(board.ESP_CS)
    rdy = DigitalInOut(board.ESP_BUSY)
    rst = DigitalInOut(board.ESP_RESET)
    spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
    return adafruit_esp32spi.ESP_SPIcontrol(spi, cs, rdy, rst), spi, cs, rdy, rst


def _deinit_esp(esp, spi, cs, rdy, rst):
    for fn in (esp.reset, spi.deinit):
        try: fn()
        except Exception: pass
    for pin in (cs, rdy, rst):
        try: pin.deinit()
        except Exception: pass
    time.sleep(1)


def _connect(esp, max_retries=6):
    retries = 0
    while not esp.is_connected:
        try:
            esp.connect_AP(secrets["ssid"], secrets["password"])
        except RuntimeError:
            retries += 1
            if retries >= max_retries:
                raise RuntimeError("WiFi unavailable")


def _epoch_now():
    try:
        return int(time.mktime(time.localtime()))
    except Exception:
        return 0


def _sync_time(esp):
    try:
        pool = adafruit_connection_manager.get_radio_socketpool(esp)
        ntp = adafruit_ntp.NTP(pool, tz_offset=0, cache_seconds=3600)
        rtc.RTC().datetime = ntp.datetime
        now = _epoch_now()
        print("NTP synced:", now)
        return now
    except Exception as e:
        print("NTP error:", e)
        return 0


def _load_retry_at():
    state = _load_state() or {}
    retry_at = int(state.get(USAGE_RETRY_KEY, 0) or 0)
    if not retry_at:
        retry_at = int(state.get("retry_at", 0) or 0)
    return retry_at


def _save_retry_at(retry_at):
    state = _load_state()
    state[USAGE_RETRY_KEY] = int(retry_at or 0)
    state["retry_at"] = 0
    _save_state(state)


def _clear_retry_state():
    state = _load_state()
    state[USAGE_RETRY_KEY] = 0
    state["retry_at"] = 0
    _save_state(state)


def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    return state


def _save_state(state):
    try:
        data = json.dumps(state)
        with open(STATE_FILE, "w") as f:
            f.write(data)
            f.flush()
    except Exception as e:
        print("State save error:", e)


def _save_state_value(key, value):
    state = _load_state()
    state[key] = value
    _save_state(state)


def _load_cached_usage():
    state = _load_state()
    usage = state.get("last_usage")
    if usage:
        # Backward compatibility with Claude-only cached state
        if isinstance(usage, dict) and "five_h" in usage and "claude" not in usage:
            usage = {"claude": usage, "codex": None}
        return usage, int(state.get("last_success_at", 0) or 0), int(state.get("last_fetch_at", 0) or 0)
    return None, 0, int(state.get("last_fetch_at", 0) or 0)


def _save_fetch_at(epoch):
    if epoch:
        _save_state_value("last_fetch_at", int(epoch))


def _save_usage_state(usage, epoch):
    state = _load_state()
    state["last_usage"] = usage
    state["last_success_at"] = int(epoch or 0)
    state["last_fetch_at"] = int(epoch or 0)
    state["retry_at"] = 0
    state[USAGE_RETRY_KEY] = 0
    _save_state(state)


def _epoch_to_mono(epoch, now_epoch):
    if epoch and now_epoch:
        return time.monotonic() - max(0, now_epoch - epoch)
    return time.monotonic()



def connect_wifi():
    pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.2)
    pixel[0] = (255, 100, 0)
    esp, spi, cs, rdy, rst = _init_esp()
    esp.reset()
    time.sleep(1)
    _connect(esp)
    pixel[0] = (0, 40, 0)
    time.sleep(0.5)
    pool = adafruit_connection_manager.get_radio_socketpool(esp)
    ssl  = adafruit_connection_manager.get_radio_ssl_context(esp)
    return adafruit_requests.Session(pool, ssl), pixel, esp, spi, cs, rdy, rst


def reconnect_wifi(esp, spi, cs, rdy, rst, pixel):
    print("Reconnecting...")
    pixel[0] = (255, 100, 0)
    _deinit_esp(esp, spi, cs, rdy, rst)
    esp, spi, cs, rdy, rst = _init_esp()
    esp.reset()
    time.sleep(1)
    _connect(esp)
    pixel[0] = (0, 40, 0)
    pool = adafruit_connection_manager.get_radio_socketpool(esp)
    ssl  = adafruit_connection_manager.get_radio_ssl_context(esp)
    return adafruit_requests.Session(pool, ssl), esp, spi, cs, rdy, rst


# ── data ──────────────────────────────────────────────────────────────────────

def _header(headers, name):
    try:
        for key in headers:
            if key.lower() == name:
                return headers[key]
    except Exception:
        pass
    return None


def _retry_after(headers):
    value = _header(headers or {}, "retry-after")
    try:
        return int(value or 0)
    except Exception:
        return 0


def _rate_limited(headers, msg="Rate limited"):
    return {
        "error": "rate_limited",
        "message": msg,
        "retry_after": _clamp_wait(_retry_after(headers)),
    }


def _model_usage_value(payload):
    if not isinstance(payload, dict):
        return 0
    return int(payload.get("fable", payload.get("sonnet", 0)) or 0)


def _has_legacy_model_cache(payload):
    if not isinstance(payload, dict):
        return False
    claude = payload.get("claude", payload)
    return isinstance(claude, dict) and "fable" not in claude and "sonnet" in claude


def fetch_usage(req):
    gc.collect()
    try:
        r = req.get(USAGE_URL)
        status = getattr(r, "status_code", 0)
        headers = getattr(r, "headers", {}) or {}
        data = r.json()
        r.close()
        del r
        gc.collect()

        if status == 429:
            msg = (data.get("error") or {}).get("message", "Rate limited")
            print("Proxy:", msg, "retry after", _retry_after(headers), "s")
            return _rate_limited(headers, msg)

        if status != 200 or "error" in data:
            print("Proxy error:", status)
            return None

        # Unified /all-usage response
        if "claude" in data or "codex" in data:
            claude = data.get("claude") if isinstance(data.get("claude"), dict) and "error" not in data.get("claude") else None
            codex  = data.get("codex") if isinstance(data.get("codex"), dict) and "error" not in data.get("codex") else None
            return {
                "claude": claude,
                "codex":  codex,
                "updated_at": data.get("updated_at", _epoch_now()),
            }

        # Legacy /claude-usage fallback
        return {
            "claude": {
                "five_h":            int(data.get("five_h", 0) or 0),
                "five_h_resets_in":  int(data.get("five_h_resets_in", 0) or 0),
                "seven_d":           int(data.get("seven_d", 0) or 0),
                "seven_d_resets_in": int(data.get("seven_d_resets_in", 0) or 0),
                "fable":             _model_usage_value(data),
                "extra_used":        data.get("extra_used", 0) or 0,
                "extra_lim":         data.get("extra_lim", 0) or 0,
                "extra_pct":         int(data.get("extra_pct", 0) or 0),
            },
            "codex": None,
            "updated_at": _epoch_now(),
        }
    except Exception as e:
        print("Fetch error:", e)
        gc.collect()
        return None


# ── display ───────────────────────────────────────────────────────────────────

def _rect(x, y, w, h, color):
    bm  = displayio.Bitmap(w, h, 1)
    pal = displayio.Palette(1)
    pal[0] = color
    return displayio.TileGrid(bm, pixel_shader=pal, x=x, y=y)


def _lbl(text, x, y, color, scale=SCALE):
    return label.Label(FONT if FONT is not None else terminalio.FONT, text=text, color=color, x=x, y=y, scale=scale)


def _fmt_wait(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return str(seconds) + "s"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return str(minutes) + "m"
    return str((minutes + 59) // 60) + "h"


def _fmt_reset(seconds):
    if not seconds:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h >= 12: return "r" + str(max(1, (h + 12) // 24)) + "d"  # r1d..r7d
    if h > 0:   return "r" + str(h) + "h"                        # r1h..r11h
    return "r" + str(m) + "m"                                     # r1m..r59m


def _set_right(lbl_obj, text):
    lbl_obj.text = text
    lbl_obj.x = _RIGHT - len(text) * 6  # right-aligned at same edge as pct


def _base():
    board.DISPLAY.root_group = displayio.Group()
    gc.collect()
    g = displayio.Group()
    g.append(_rect(0, 0, W, H, BLACK))
    return g


def _hline(left, fill, right):
    return left + fill * (COLS - 2) + right


def _bar_row(lbl_str, pct, reset_secs):
    BARS = 10
    filled = BARS * pct // 100
    bar = "█" * filled + "░" * (BARS - filled)
    p = str(pct)
    pct_str = (" " if pct < 10 else "") + p + "%"
    reset_str = "  " + _fmt_reset(reset_secs) if reset_secs else ""
    return lbl_str + " " + bar + "  " + pct_str + reset_str


def _mid_y():
    """Vertical center of content area (between title sep and bottom border)."""
    return (_Y_BSEP + _Y_BBOT) // 2 - 10


def loading_screen():
    g = _base()
    _draw_frame(g)
    g.append(_lbl("Connecting...", _TX_C, _mid_y(), SUB))
    return g


def time_sync_screen():
    g = _base()
    _draw_frame(g)
    g.append(_lbl("Syncing time", _TX_C, _mid_y(), SUB))
    return g


def waiting_screen(seconds, reason="startup"):
    g = _base()
    _draw_frame(g)
    mid = _mid_y()
    g.append(_lbl("Waiting: " + reason, _TX_C, mid - 16, SUB))
    countdown_lbl = _lbl("First fetch in " + _fmt_wait(seconds), _TX_C, mid + 8, DIM)
    g.append(countdown_lbl)
    return g, countdown_lbl


def error_screen(msg="No data"):
    g = _base()
    _draw_frame(g)
    g.append(_lbl(msg, _TX_C, _mid_y(), RED))
    return g


def fatal_screen(msg):
    g = _base()
    _draw_frame(g)
    g.append(_lbl("Fatal: " + str(msg)[:28], _TX_C, _mid_y(), RED))
    return g


def rate_limit_screen(seconds, stale_u=None, view_mode=VIEW_CLAUDE, reason="429 cooldown"):
    if stale_u:
        g, age_lbl = usage_screen(stale_u, view_mode=view_mode, stale=True)
        if age_lbl is not None:
            _set_right(age_lbl, "wait " + _fmt_wait(seconds))
        return g, age_lbl
    g = _base()
    age_lbl = _draw_frame(g)
    mid = _mid_y()
    g.append(_lbl("Rate limited", _TX_C, mid - 16, AMBER))
    countdown_lbl = _lbl("Retry in " + _fmt_wait(seconds), _TX_C, mid + 8, DIM)
    g.append(countdown_lbl)
    return g, countdown_lbl


def refreshing_screen():
    g = _base()
    _draw_frame(g)
    g.append(_lbl("Refreshing...", _TX_C, _mid_y(), SUB))
    return g


def next_refresh_screen(seconds):
    g = _base()
    _draw_frame(g)
    mid = _mid_y()
    g.append(_lbl("Too soon", _TX_C, mid - 16, SUB))
    countdown_lbl = _lbl("Next refresh in " + _fmt_wait(seconds), _TX_C, mid + 8, DIM)
    g.append(countdown_lbl)
    return g, countdown_lbl


_BL   = 6            # left border x (6px inset)
_Y_BTOP = 2          # top border y (2px inset)
_Y_BSEP = 50         # title band
_Y_STAT_SEP = 138    # divider below the Codex stat tiles
_Y_BBOT = H - 2      # bottom border at screen edge

_ROW_SLOT = 42
_Y_ROW1 = _Y_BSEP + 22

_TX_T = _BL + CW     # title indent: 1 char inside left border
_TX_C = _BL + CW     # content indent: 1 char inside left border
_RIGHT = W - 2 - CW  # right content edge: 320-2-12=306px


def _draw_frame(g, title_main="CLAUDE", title_sub="usage", color=ORANGE, stale=False):
    """Shared border + branding header. Returns age_lbl."""
    g.append(_rect(_BL,   _Y_BTOP, W - _BL, 2, color))
    g.append(_rect(_BL,   _Y_BBOT, W - _BL, 2, color))
    g.append(_rect(_BL,   _Y_BTOP, 2, _Y_BBOT - _Y_BTOP + 2, color))
    g.append(_rect(W - 2, _Y_BTOP, 2, _Y_BBOT - _Y_BTOP + 2, color))
    g.append(_rect(_BL,   _Y_BSEP, W - _BL, 2, color))

    header_mid = (_Y_BTOP + _Y_BSEP) // 2
    CL_W = len(title_main) * 6 * 3

    # AIDEV-NOTE: title_sub is capped at 8 chars so scale=2 text never extends past x=218px or overlaps age_lbl.
    if len(title_sub) > 8:
        title_sub = title_sub[:8]

    title_lbl = _lbl(title_main, _TX_T, header_mid, color, scale=3)
    title_lbl.anchor_point = (0.0, 0.5)
    title_lbl.anchored_position = (_TX_T, header_mid)
    g.append(title_lbl)

    subtitle_lbl = _lbl(title_sub, _TX_T + CL_W + 8, header_mid, WHITE)
    subtitle_lbl.anchor_point = (0.0, 0.5)
    subtitle_lbl.anchored_position = (_TX_T + CL_W + 8, header_mid)
    g.append(subtitle_lbl)

    age_lbl = _lbl("STALE" if stale else "", 0, header_mid, AMBER, scale=1)
    age_lbl.anchor_point = (0.0, 0.5)
    age_lbl.anchored_position = (0, header_mid)
    if stale:
        _set_right(age_lbl, "STALE")
    g.append(age_lbl)
    return age_lbl


def _segmented_bar(x, y, pct, col, na=False):
    """Build one compact bitmap bar with solid cells and black gaps."""
    # AIDEV-NOTE: One bitmap per bar avoids exhausting the constrained heap with per-cell TileGrids.
    bars = 16
    cell_w = 10
    gap = 2
    height = 12
    width = bars * cell_w + (bars - 1) * gap
    filled = 0 if na or pct is None else max(0, min(bars, bars * int(pct) // 100))
    if not na and pct and filled == 0:
        filled = 1

    bitmap = displayio.Bitmap(width, height, 3)
    palette = displayio.Palette(3)
    palette[0] = BLACK
    palette[1] = DIM
    palette[2] = col

    for cell in range(bars):
        color_index = 2 if cell < filled else 1
        left = cell * (cell_w + gap)
        for px in range(left, left + cell_w):
            for py in range(height):
                bitmap[px, py] = color_index

    return displayio.TileGrid(bitmap, pixel_shader=palette, x=x, y=y)


def _bar_label(g, y, name, pct, reset_secs, col, sub_text=None, na=False):
    """Bar row: label | solid segmented bar | pct | optional details."""
    if na or pct is None:
        pct_s = " --%"
        pct_col = SUB
    else:
        pct = max(0, min(100, int(pct)))
        pct_s = (" " if pct < 10 else "") + str(pct) + "%"
        pct_col = WHITE

    pct_x = _RIGHT - 3 * CW
    BX    = _TX_C + 3 * CW + CW + 4

    g.append(_lbl(name,  _TX_C, y, WHITE))
    g.append(_segmented_bar(BX, y - 8, pct, col, na=na))
    g.append(_lbl(pct_s, pct_x, y, pct_col))

    reset_text = _fmt_reset(reset_secs) if reset_secs else ""
    detail = (sub_text or "") + (" " if reset_text and sub_text else "") + reset_text
    if detail:
        detail_x = _RIGHT - len(detail) * 6
        if sub_text:
            sub_col = GREEN if sub_text == "ACTIVE" else AMBER
            g.append(_lbl(sub_text, detail_x, y + CH, sub_col, scale=1))
        if reset_text:
            reset_x = detail_x + (len(sub_text or "") + (1 if sub_text else 0)) * 6
            g.append(_lbl(reset_text, reset_x, y + CH, AMBER, scale=1))


def _limit_status(u, prefix):
    if u.get(prefix + "_active", False):
        return "ACTIVE"
    severity = str(u.get(prefix + "_severity", "") or "").upper()
    return severity if severity and severity != "NORMAL" else None


def claude_screen(u, stale=False):
    g = _base()
    five_h_reset  = int(u.get("five_h_resets_in", 0) or 0)
    seven_d_reset = int(u.get("seven_d_resets_in", 0) or 0)
    fable_reset   = int(u.get("fable_resets_in", 0) or 0)

    used_d = (u.get("extra_used", 0) or 0) / 100
    lim_d  = (u.get("extra_lim", 0) or 0) / 100
    cents  = int((used_d * 100) % 100)
    spent  = "$" + str(int(used_d)) + "." + ("0" if cents < 10 else "") + str(cents)
    lim_k  = "/$" + (str(int(lim_d // 1000)) + "k" if lim_d >= 1000 else str(int(lim_d)))
    dollar_str = spent + lim_k

    age_lbl = _draw_frame(g, title_main="CLAUDE", title_sub="usage", color=ORANGE, stale=stale)

    Y_R1  = _Y_ROW1
    Y_R2  = Y_R1 + _ROW_SLOT
    Y_R3  = Y_R2 + _ROW_SLOT
    Y_R4  = Y_R3 + _ROW_SLOT

    five_h    = u.get("five_h", 0) or 0
    seven_d   = u.get("seven_d", 0) or 0
    fable     = _model_usage_value(u)
    extra_pct = u.get("extra_pct", 0) or 0

    _bar_label(g, Y_R1, "5h ", five_h, five_h_reset, bar_color(five_h), _limit_status(u, "five_h"))
    _bar_label(g, Y_R2, "7d ", seven_d, seven_d_reset, bar_color(seven_d), _limit_status(u, "seven_d"))
    _bar_label(g, Y_R3, "fbl", fable, fable_reset, bar_color(fable), _limit_status(u, "fable"))
    _bar_label(g, Y_R4, "xtr", extra_pct, 0,             bar_color(extra_pct), dollar_str)

    return g, age_lbl


def _text_usage_row(g, y, name, value, detail):
    g.append(_lbl(name, _TX_C, y, WHITE))
    g.append(_lbl(value, _RIGHT - len(value) * CW, y, WHITE))
    if detail:
        g.append(_lbl(detail, _RIGHT - len(detail) * 6, y + CH, AMBER, scale=1))


def _usage_stat(g, center, title, value, detail, color):
    g.append(_lbl(title, center - len(title) * 3, 68, WHITE, scale=1))
    value_scale = 3 if len(value) <= 7 else 2
    g.append(_lbl(value, center - len(value) * 3 * value_scale, 94, color, scale=value_scale))
    if detail:
        g.append(_lbl(detail, center - len(detail) * 3, 120, SUB, scale=1))


def codex_screen(u, stale=False):
    g = _base()
    five_h        = u.get("five_h")
    five_h_reset  = int(u.get("five_h_resets_in", 0) or 0)
    seven_d       = u.get("seven_d")
    seven_d_reset = int(u.get("seven_d_resets_in", 0) or 0)
    reset_credits = int(u.get("reset_credits", 0) or 0)
    limit_reached = bool(u.get("limit_reached", False))

    age_lbl = _draw_frame(g, title_main="CODEX", title_sub="usage", color=TEAL, stale=stale)

    Y_R1  = _Y_ROW1
    Y_R2  = Y_R1 + _ROW_SLOT
    Y_R3  = Y_R2 + _ROW_SLOT
    Y_R4  = Y_R3 + _ROW_SLOT

    applicable = u.get("applicable_reset_credits")
    if five_h is None:
        points = u.get("daily_points")
        day_text = "collecting" if points is None else "+" + str(int(points)) + " pts"
        midpoint = (_TX_C + _RIGHT) // 2
        left_center = (_TX_C + midpoint) // 2
        right_center = (midpoint + _RIGHT) // 2
        g.append(_rect(midpoint, 62, 1, 68, DIM))
        _usage_stat(g, left_center, "TODAY", day_text, "observed today", TEAL)
        detail = "" if applicable is None else str(int(applicable)) + " usable now"
        _usage_stat(g, right_center, "RESETS", str(reset_credits), detail, AMBER)
        g.append(_rect(_BL, _Y_STAT_SEP, W - _BL, 2, TEAL))
        weekly_y = Y_R3
    else:
        # Preserve the four-row layout for accounts with a short-term quota.
        _bar_label(g, Y_R1, "5h ", five_h, five_h_reset, bar_color(five_h))
        detail = "" if applicable is None else str(int(applicable)) + " applicable now"
        _text_usage_row(g, Y_R3, "rst", str(reset_credits) + " banked", detail)
        weekly_y = Y_R2

    if seven_d is not None:
        _bar_label(g, weekly_y, "7d ", seven_d, seven_d_reset, bar_color(seven_d))
    else:
        _bar_label(g, weekly_y, "7d ", 0, 0, DIM, na=True)

    # Row 4: Status / Limit
    # AIDEV-NOTE: Previously fell back to seven_d, causing Row 4 ("lim") to duplicate Row 2 ("7d").
    # When not limit-reached, status_pct should be 0 (active), not mirroring the 7-day quota.
    status_pct = 100 if limit_reached else 0
    status_col = RED if limit_reached else GREEN
    status_sub = "LIMIT REACHED" if limit_reached else "ACTIVE"
    _bar_label(g, Y_R4, "lim", status_pct, 0, status_col, status_sub)

    return g, age_lbl


def usage_screen(data, view_mode=VIEW_CLAUDE, stale=False):
    if not data:
        return error_screen("No data"), None

    claude_data = data.get("claude") if isinstance(data, dict) else None
    codex_data  = data.get("codex")  if isinstance(data, dict) else None

    # Handle direct/flat dicts
    if claude_data is None and codex_data is None and isinstance(data, dict):
        if "plan" in data:
            return codex_screen(data, stale=stale)
        return claude_screen(data, stale=stale)

    if view_mode == VIEW_CODEX and codex_data:
        return codex_screen(codex_data, stale=stale)
    if claude_data:
        return claude_screen(claude_data, stale=stale)
    if codex_data:
        return codex_screen(codex_data, stale=stale)

    return error_screen("No active service"), None


# ── main ──────────────────────────────────────────────────────────────────────

try:
    board.DISPLAY.root_group = loading_screen()

    req, pixel, esp, spi, cs, rdy, rst = connect_wifi()
    board.DISPLAY.root_group = time_sync_screen()
    synced_epoch = _sync_time(esp)
    _init_font()
    display    = board.DISPLAY
    fail_count = 0
    fetch_time    = time.monotonic()
    age_lbl       = None
    refresh_secs  = REFRESH_MIN
    last_data     = {}
    last_usage     = None
    last_success_at = 0
    last_fetch_at = 0
    last_refresh  = time.monotonic()
    last_carousel = time.monotonic()
    current_view  = VIEW_CLAUDE

    cached_usage, cached_success_at, cached_fetch_at = _load_cached_usage()
    legacy_model_cache = _has_legacy_model_cache(cached_usage)
    if cached_usage:
        last_usage = cached_usage
        last_success_at = cached_success_at
        fetch_time = _epoch_to_mono(cached_success_at, synced_epoch)
    if cached_fetch_at:
        last_fetch_at = cached_fetch_at
        last_refresh = _epoch_to_mono(cached_fetch_at, synced_epoch)
        if legacy_model_cache:
            last_refresh = time.monotonic() - REFRESH_MIN

    usage_retry_at = _load_retry_at()
    usage_wait = usage_retry_at - synced_epoch if synced_epoch else 0
    if usage_wait > 0:
        first_fetch_at = last_refresh
    elif synced_epoch:
        first_fetch_at = last_refresh + STARTUP_READY_GRACE
    else:
        first_fetch_at = last_refresh + STARTUP_GRACE

    rate_limit_until = 0
    rate_limit_lbl = None
    startup_wait_lbl = None
    next_refresh_lbl = None
    next_refresh_until = 0
    startup_wait_reason = "startup grace" if synced_epoch else "time sync failed"
    rate_limit_reason = "429 cooldown"
    rate_limit_count = 0

    if usage_wait > 0:
        rate_limit_until = time.monotonic() + usage_wait
        refresh_secs = max(REFRESH_MIN, usage_wait)
        rate_limit_reason = "saved cooldown"
    elif last_usage:
        first_fetch_at = last_refresh + REFRESH_MIN
        screen, age_lbl = usage_screen(last_usage, view_mode=current_view, stale=True)
        display.root_group = screen

    ts = adafruit_touchscreen.Touchscreen(
        board.TOUCH_XL, board.TOUCH_XR,
        board.TOUCH_YD, board.TOUCH_YU,
        calibration=((5200, 59000), (5800, 57000)),
        size=(320, 240),
    )

    while True:
        touch_pt = None
        for _ in range(10):
            p = ts.touch_point
            if p:
                touch_pt = p
                break
            time.sleep(0.1)

        now = time.monotonic()
        startup_wait = now < first_fetch_at and last_usage is None
        first_fetch_due = last_usage is None and now >= first_fetch_at and now >= rate_limit_until
        due = (not startup_wait) and (first_fetch_due or now - last_refresh >= refresh_secs)

        if startup_wait and startup_wait_lbl is None:
            screen, startup_wait_lbl = waiting_screen(int(first_fetch_at - now), startup_wait_reason)
            display.root_group = screen
        if (not startup_wait) and startup_wait_lbl is not None:
            startup_wait_lbl = None
        if (not startup_wait) and now < rate_limit_until and rate_limit_lbl is None:
            screen, rate_limit_lbl = rate_limit_screen(int(rate_limit_until - now), last_usage, view_mode=current_view, reason=rate_limit_reason)
            display.root_group = screen

        # Auto-carousel between Claude and Codex views
        has_both = bool(last_usage and last_usage.get("claude") and last_usage.get("codex"))
        if has_both and not startup_wait and rate_limit_lbl is None and next_refresh_lbl is None:
            if now - last_carousel >= CAROUSEL_SECS:
                current_view = VIEW_CODEX if current_view == VIEW_CLAUDE else VIEW_CLAUDE
                screen, age_lbl = usage_screen(last_usage, view_mode=current_view, stale=(now - last_refresh >= REFRESH_MIN))
                display.root_group = screen
                last_carousel = now

        # Handle Touch or Scheduled Refresh
        if touch_pt or due:
            if startup_wait:
                continue

            # Check if touch is a view toggle (body tap) vs manual refresh (top-right header tap)
            is_manual_refresh = due
            if touch_pt and not due:
                x, y, _ = touch_pt
                # Header right quadrant = manual refresh action
                if y < 60 and x > 180:
                    is_manual_refresh = True
                else:
                    # Tap body = instant view toggle
                    if has_both:
                        current_view = VIEW_CODEX if current_view == VIEW_CLAUDE else VIEW_CLAUDE
                        screen, age_lbl = usage_screen(last_usage, view_mode=current_view, stale=(now - last_refresh >= REFRESH_MIN))
                        display.root_group = screen
                        last_carousel = now
                    while ts.touch_point:
                        time.sleep(0.05)
                    continue

            if is_manual_refresh:
                startup_wait_lbl = None
                pixel[0] = (0, 0, 20)
                if touch_pt and not due and now - last_refresh < MANUAL_REFRESH_MIN:
                    wait = int(MANUAL_REFRESH_MIN - (now - last_refresh))
                    next_refresh_until = time.monotonic() + wait
                    screen, next_refresh_lbl = next_refresh_screen(wait)
                    display.root_group = screen
                    pixel[0] = (0, 0, 0)
                    while ts.touch_point:
                        time.sleep(0.05)
                    continue
                if touch_pt and age_lbl is not None:
                    _set_right(age_lbl, "refreshing...")
                elif age_lbl is None and rate_limit_lbl is None:
                    display.root_group = refreshing_screen()
                gc.collect()
                now = time.monotonic()
                if now < rate_limit_until:
                    wait = int(rate_limit_until - now)
                    refresh_secs = max(REFRESH_MIN, wait)
                    screen, rate_limit_lbl = rate_limit_screen(wait, last_usage, view_mode=current_view, reason=rate_limit_reason)
                    display.root_group = screen
                    age_lbl = None
                else:
                    epoch_now = _epoch_now()
                    _save_fetch_at(epoch_now)
                    u = fetch_usage(req)
                    if u and u.get("error") == "rate_limited":
                        fail_count = 0
                        rate_limit_count += 1
                        wait = _clamp_wait(u.get("retry_after", 0))
                        if rate_limit_count > 1 and wait < REFRESH_MAX:
                            wait = min(REFRESH_MAX, wait * rate_limit_count)
                        rate_limit_until = time.monotonic() + wait
                        if synced_epoch:
                            _save_retry_at(_epoch_now() + wait)
                        rate_limit_reason = "429 from proxy"
                        refresh_secs = wait
                        screen, rate_limit_lbl = rate_limit_screen(wait, last_usage, view_mode=current_view, reason=rate_limit_reason)
                        display.root_group = screen
                        age_lbl = None
                    elif u:
                        rate_limit_until = 0
                        _clear_retry_state()
                        rate_limit_lbl = None
                        rate_limit_count = 0
                        fail_count = 0
                        refresh_secs = REFRESH_MIN
                        last_usage = u
                        last_success_at = _epoch_now()
                        _save_usage_state(u, last_success_at)
                        screen, age_lbl = usage_screen(u, view_mode=current_view)
                        display.root_group = screen
                        fetch_time = time.monotonic()
                        last_carousel = time.monotonic()
                    else:
                        fail_count += 1
                        if fail_count >= 2:
                            try:
                                req, esp, spi, cs, rdy, rst = reconnect_wifi(esp, spi, cs, rdy, rst, pixel)
                            except RuntimeError:
                                pixel[0] = (0, 0, 0)
                                fail_count = 0
                                age_lbl = None
                                rate_limit_lbl = None
                                display.root_group = error_screen("WiFi offline")
                                last_refresh = time.monotonic()
                                continue
                            fail_count = 0
                        if last_usage:
                            if age_lbl is None:
                                screen, age_lbl = usage_screen(last_usage, view_mode=current_view, stale=True)
                                display.root_group = screen
                        else:
                            display.root_group = error_screen("proxy offline")
                pixel[0] = (0, 0, 0)
                last_refresh = time.monotonic()
                while ts.touch_point:
                    time.sleep(0.05)

        # Live updates each tick
        if age_lbl is not None and age_lbl.text != "refreshing...":
            elapsed = int(time.monotonic() - fetch_time)
            txt = "< 1m ago" if elapsed < 60 else str(elapsed // 60) + "m ago"
            _set_right(age_lbl, txt)
        if rate_limit_lbl is not None:
            wait = max(0, int(rate_limit_until - time.monotonic()))
            rate_limit_lbl.text = "wait " + _fmt_wait(wait)
        if startup_wait_lbl is not None:
            startup_wait_lbl.text = "First fetch in " + _fmt_wait(max(0, int(first_fetch_at - time.monotonic())))
        if next_refresh_lbl is not None:
            wait = int(next_refresh_until - time.monotonic())
            if wait <= 0:
                next_refresh_lbl = None
                if last_usage:
                    screen, age_lbl = usage_screen(last_usage, view_mode=current_view, stale=True)
                    display.root_group = screen
            else:
                next_refresh_lbl.text = "Next refresh in " + _fmt_wait(wait)

except Exception as e:
    print("Fatal:", e)
    try:
        board.DISPLAY.root_group = fatal_screen(e)
    except Exception:
        pass
    time.sleep(5)
    supervisor.reload()

import sys
import types
import unittest


def load_device_code(settings_obj=None):
    """Compile code.py with a stubbed environment; return its module namespace."""
    saved = dict(sys.modules)
    src = open("projects/usage-dashboard/code.py").read()
    module = types.ModuleType("device_code")
    stubs = {
        "gc": types.SimpleNamespace(collect=lambda: None),
        "json": __import__("json"),
        "time": __import__("time"),
        "board": types.SimpleNamespace(DISPLAY=None),
        "displayio": types.SimpleNamespace(Group=object, Bitmap=object, Palette=object, TileGrid=object),
        "busio": types.SimpleNamespace(SPI=object),
        "supervisor": types.SimpleNamespace(runtime=types.SimpleNamespace(run_reason=None, usb_connected=True, autoreload=True), reload=lambda: None),
        "rtc": types.SimpleNamespace(RTC=object),
        "digitalio": types.SimpleNamespace(DigitalInOut=object),
        "neopixel": types.SimpleNamespace(NeoPixel=object),
        "adafruit_requests": types.SimpleNamespace(Session=object),
        "adafruit_connection_manager": types.SimpleNamespace(get_radio_socketpool=lambda esp: None),
        "adafruit_ntp": types.SimpleNamespace(NTP=object),
        "adafruit_esp32spi": types.SimpleNamespace(adafruit_esp32spi=types.SimpleNamespace(ESP_SPIcontrol=object)),
        "terminalio": types.SimpleNamespace(FONT=None),
        "adafruit_display_text": types.SimpleNamespace(label=types.SimpleNamespace(Label=object)),
        "adafruit_bitmap_font": types.SimpleNamespace(bitmap_font=types.SimpleNamespace(load_font=lambda p: None)),
        "adafruit_touchscreen": types.SimpleNamespace(Touchscreen=object),
        "secrets": types.SimpleNamespace(secrets={"proxy_url": "http://x:9292"}),
        "settings": settings_obj if settings_obj is not None else {},
    }
    sys.modules.update(stubs)
    try:
        exec(compile(src, "code.py", "exec"), module.__dict__)
    finally:
        sys.modules.clear()
        sys.modules.update(saved)
    return module


class SettingsDefaultsTests(unittest.TestCase):
    def test_defaults_when_settings_missing(self):
        code = load_device_code(settings_obj={})
        self.assertEqual(code.REFRESH_MIN, 900)
        self.assertEqual(code.SWIPE_MIN_PX, 80)
        self.assertEqual(code.CAROUSEL_SECS, 60)
        self.assertEqual(code.REQ_TIMEOUT, 30)

    def test_settings_override(self):
        code = load_device_code(settings_obj={
            "USAGE_SWIPE_MIN_PX": 40,
            "USAGE_CAROUSEL_SECS": 30,
            "USAGE_REFRESH_MIN": 300,
        })
        self.assertEqual(code.SWIPE_MIN_PX, 40)
        self.assertEqual(code.CAROUSEL_SECS, 30)
        self.assertEqual(code.REFRESH_MIN, 300)
        # untouched values keep defaults
        self.assertEqual(code.SWIPE_TAP_MS, 600)

    def test_invalid_settings_fall_back(self):
        code = load_device_code(settings_obj={"USAGE_SWIPE_MIN_PX": "not-a-number"})
        self.assertEqual(code.SWIPE_MIN_PX, 80)


if __name__ == "__main__":
    unittest.main()

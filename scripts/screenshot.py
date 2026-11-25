"""Reconstruct the PyPortal's live screen as a PNG, for change verification.

Usage: devenv shell -- screenshot [PORT]

Sequence (order enforced here, not by the operator):
  1. Confirm the app is running (probe serial; refuse to send the trigger
     byte into a REPL, where it would raise NameError).
  2. Send the trigger byte 'd' + newline. The app walks its live displayio
     scene graph and writes /dump_display.json (RLE-encoded, heap-safe).
  3. Wait for the 'display dumped' ack on serial.
  4. Remount the CIRCUITPY volume so macOS sees the file the app just wrote
     (USB mass storage shows a boot-time snapshot otherwise).
  5. Parse the dump, reconstruct a 320x240 PNG, print every label with its
     position and color so assertions can be made against the output.

Outputs: /tmp/device_screenshot.png and a JSON summary on stdout.
Exit codes: 0 ok; 1 wrong device state; 2 trigger/ack failure; 3 no dump.
"""

import json
import subprocess
import sys
import time

from PIL import Image

MOUNT = "/Volumes/CIRCUITPY"
DUMP_PATH = MOUNT + "/dump_display.json"
OUT_PNG = "/tmp/device_screenshot.png"


def pick_port():
    if len(sys.argv) > 1:
        return sys.argv[1]
    import glob
    cands = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    return cands[0] if cands else None


def open_port(port):
    import serial
    return serial.Serial(port, 115200, timeout=1)


def check_running(port):
    """The trigger only works while code.py runs. Sending 'd' at the REPL
    raises NameError, so probe first: a running app ignores a bare newline
    (or echoes nothing); a REPL answers its prompt."""
    s = open_port(port)
    try:
        s.reset_input_buffer()
        s.write(b"\r\n")
        time.sleep(1.0)
        buf = b""
        t0 = time.time()
        while time.time() - t0 < 1.5:
            n = s.in_waiting
            if n:
                buf += s.read(n)
        if b">>>" in buf:
            return False, "device is at the REPL — reload first: devenv shell -- reload"
        return True, "app running (no REPL prompt)"
    finally:
        s.close()


def trigger_and_wait(port, seconds=20.0):
    s = open_port(port)
    try:
        s.reset_input_buffer()
        s.write(b"d\r\n")
        t0 = time.time()
        buf = b""
        while time.time() - t0 < seconds:
            n = s.in_waiting
            if n:
                buf += s.read(n)
                if b"display dumped" in buf:
                    return True, f"dump written in {time.time() - t0:.1f}s"
                if b"dump error" in buf:
                    return False, "device reported dump error: " + buf.decode(errors="replace").strip()[-120:]
        return False, "no 'display dumped' ack — app may not be running the current build"
    finally:
        s.close()


def remount():
    subprocess.run(["diskutil", "unmount", MOUNT], capture_output=True)
    time.sleep(2)
    r = subprocess.run(["diskutil", "mount", "disk8s1"], capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout.strip():
        return False, "diskutil mount failed: " + r.stdout + r.stderr
    time.sleep(2)
    return True, "remounted"


def reconstruct(items):
    img = Image.new("RGB", (320, 240), (0, 0, 0))
    for it in items:
        if it.get("kind") != "TileGrid" or "rle" not in it:
            continue
        pal = it.get("palette")
        if not pal:
            continue
        x, y, w, h = it["x"], it["y"], it["w"], it["h"]
        cx = cy = 0
        for v, run in it["rle"]:
            for _ in range(run):
                if cy >= h:
                    break
                if v < len(pal):
                    img.putpixel((x + cx, y + cy), tuple(pal[v]))
                cx += 1
                if cx >= w:
                    cx, cy = 0, cy + 1
    img.save(OUT_PNG)


def main():
    port = pick_port()
    if not port:
        print("no serial port found")
        return 1

    ok, msg = check_running(port)
    print(msg)
    if not ok:
        return 1

    ok, msg = trigger_and_wait(port)
    print(msg)
    if not ok:
        return 2

    ok, msg = remount()
    if not ok:
        print(msg)
        return 3
    try:
        with open(DUMP_PATH) as f:
            items = json.load(f)
    except FileNotFoundError:
        print("dump file not visible after remount")
        return 3

    labels = [i for i in items if i.get("text")]
    print(f"{len(items)} scene items, {len(labels)} labels")
    for l in labels:
        print(f"  {l['text']!r} at ({l['x']},{l['y']}) color {tuple(l['color'])}")
    reconstruct(items)
    print(f"wrote {OUT_PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

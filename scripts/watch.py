#!/usr/bin/env python3
"""Serial monitor for the PyPortal device.

Usage: watch [PORT] [SECONDS]

Tails the device serial console for a bounded window (default 30s) and
classifies what it sees. Exit codes make the device state deterministic:

  0  app is running (saw "code.py output:" and no exit-to-REPL markers)
  1  device is sitting at the REPL (saw "Code done running." / "Done |")
  2  no serial output at all (no device, or wrong port)
  3  app is crash-looping (saw a Traceback)

Healthy behavior: after a reload, the device prints "code.py output:" and
"NTP synced: <epoch>", then goes silent until the next 15-minute refresh.
Silence after boot is normal; silence without a boot line is not.
"""

import re
import sys
import time

import serial


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else None
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    if not port:
        import glob
        candidates = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
        if not candidates:
            print("No serial port found")
            return 2
        port = candidates[0]

    saw_boot = False
    saw_ntp = False
    saw_done = False
    saw_traceback = False

    with serial.Serial(port, 115200, timeout=1) as s:
        time.sleep(0.3)
        t0 = time.time()
        buf = b""
        probed = False
        while time.time() - t0 < window:
            if not probed and time.time() - t0 > 5 and not buf.strip():
                # Silence so far: nudge the console. A running app ignores a
                # bare newline; a device sitting at the REPL echoes its prompt.
                s.write(b"\r\n")
                probed = True
            chunk = s.read(s.in_waiting or 1)
            if chunk:
                buf += chunk
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
        # Drain any remainder without printing
        while s.in_waiting:
            buf += s.read(s.in_waiting)

    text = re.sub(r"(sk-ant|eyJ)[A-Za-z0-9._-]{8,}", "<REDACTED>", buf.decode(errors="replace"))
    saw_boot = "code.py output:" in text
    saw_ntp = "NTP synced:" in text
    saw_done = "Code done running." in text or re.search(r"Done \|", text) is not None
    saw_traceback = "Traceback" in text
    saw_repl_prompt = ">>>" in text

    print("")
    if saw_traceback:
        print("VERDICT: crash — Traceback in serial output")
        return 3
    if saw_done and not saw_boot:
        print("VERDICT: at REPL — app exited (black screen). Recover with: devenv shell -- reload")
        return 1
    if saw_repl_prompt and not saw_boot:
        print("VERDICT: at REPL — prompt echoed (black screen). Recover with: devenv shell -- reload")
        return 1
    if saw_ntp or saw_boot:
        print("VERDICT: running" + ("" if saw_ntp else " (boot seen, NTP line not yet seen)"))
        return 0
    if not text.strip():
        print(f"VERDICT: no serial output in {window:.0f}s — port silent. Reload first: devenv shell -- reload")
        return 2
    print("VERDICT: ambiguous output; reload first, then rerun watch")
    return 2


if __name__ == "__main__":
    sys.exit(main())

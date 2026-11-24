# Device deployment and testing

## Commands

All commands run as `devenv shell -- <command>`. The serial port autodetects
`/dev/cu.usbmodem*` on macOS and `/dev/ttyACM*` on Linux. Set `PYPORTAL_PORT`
to override.

| Command | Purpose |
|---|---|
| `deploy usage-dashboard` | Push `projects/usage-dashboard/code.py` to the device |
| `deploy-shared` | Push `shared/boot.py` |
| `deploy-secrets` | Push `shared/secrets.py` (never commit this file) |
| `reload` | Soft reload the device |
| `verify` | Reload, watch serial, and assert the app is running |
| `repl` | Open a raw serial terminal |
| `watch` | Tail serial output and classify the device state |

`watch` prints a verdict and exits with a code: 0 running, 1 at the REPL
(black screen), 2 silent or ambiguous, 3 crash-looping. On silence it nudges
the console with a newline: a running app ignores it, a device at the REPL
echoes its `>>>` prompt. Because a verdict is an exit code, `watch` and
`verify` are scriptable checks, not just log viewers.

`deploy` writes the file in 1KB chunks over serial, verifies the byte size,
and resets the board. This exists because `mpremote cp` fails for files larger
than about 20KB and when `boot.py` has remounted the filesystem read-only.

A healthy boot shows `code.py output:` followed by `NTP synced: <epoch>` in
the serial output.

## Detecting a stuck REPL

When the display goes black or appears frozen, the app may have exited to the
REPL. Serial then shows the terminal status line (`Done | <CircuitPython
version>`), `Code done running.`, or `Press any key to enter the REPL` instead
of `code.py output:`.

The simplest deterministic probe is `devenv shell -- verify`, which performs
the reload itself and then asserts on the boot output. A green run shows
`code.py output:` followed by `NTP synced: <epoch>` and exits 0. If the app
has exited, the verdict is `at REPL` and the exit code is 1. `watch` can be
run on its own, without reloading, to classify the device as it currently
sits.

A second check uses `mpremote`:

```sh
devenv shell -- mpremote resume exec 'import supervisor; print(supervisor.runtime.run_reason)'
```

After a successful reload the run reason is `REPL_RELOAD`. If the app has died
you get the REPL and `Code done running.` in the serial log.

Important: a healthy device prints nothing between 15-minute refresh
intervals. Serial silence alone is not a failure. Always reload first, then
look for the boot lines.

## Recovery

To recover from a stuck REPL, run `devenv shell -- reload`. This sends Ctrl-C
Ctrl-C Ctrl-D over raw serial. If the output shows `soft reboot`, `code.py
output:`, and `NTP synced:`, the device is running again.

If the reload does not take, hard reset the board with `devenv shell --
mpremote connect <port> reset`, then watch serial for the same boot lines.

If the app crashes immediately and repeatedly on boot, watch serial for the
Traceback and the file and line. A `usage_state.json` written just before a
crash can boot-loop the app. Fix the code, or delete the state over serial
(a device-only operation, no secrets involved):

```sh
devenv shell -- mpremote resume exec 'import os; os.remove("/usage_state.json")'
```

## Why the black screen happens

The app is a single `while True` loop. Any unhandled exception exits to the
supervisor and lands at the REPL, and `boot.py` blanks the display, so the
failure is invisible on screen.

Common triggers:

- A REPL user interrupt, such as Ctrl-C in `repl`.
- An exception in the main loop.
- A serial console left attached from a previous debugging session; a stray
  Ctrl-D or Ctrl-C sent by tooling exits the running app.

## Verify after deploy

During development, run `devenv shell -- deploy usage-dashboard` then
`devenv shell -- verify`, which reloads the device and asserts a green boot:
`code.py output:` and `NTP synced:` with no Traceback. After a healthy start
the device goes silent between refreshes. The refresh interval is 15 minutes
and the screen continues to show cached data. If the display goes black or
blank, run `verify` again, then recover as described above.

## Known-good indicators

On a healthy device, checked via `mpremote resume exec`:

- The RTC matches real time after the NTP sync.
- `/usage_state.json` has fresh `last_fetch_at` and `last_success_at` values,
  under about 15 minutes old:

  ```sh
  devenv shell -- mpremote resume exec 'import json; print(json.load(open("/usage_state.json"))["last_success_at"])'
  ```

  Compare the value against the host clock.

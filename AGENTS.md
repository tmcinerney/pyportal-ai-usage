# Repository guidance

## Scope

This repository contains one product: a PyPortal dashboard for Claude Code and
Codex usage. Keep changes within that boundary.

## Development

- Prefer `devenv shell -- <command>` for Python, `mpremote`, `circup`, tests, and
  validation.
- Autodetect `/dev/cu.usbmodem*` on macOS and `/dev/ttyACM*` on Linux; use
  `PYPORTAL_PORT` for an explicit device.
- Compile-check `projects/usage-dashboard/code.py` with CPython after edits.
- Run `python -m unittest discover -s tests` for host-side behavior.
- Run `git diff --check` before committing.

## Credentials

- Never commit `shared/secrets.py`, `secrets.py`, provider auth files, token
  dumps, or captured API responses.
- Never print token or Wi-Fi credential values.
- Keep provider credentials on the proxy host; the device receives only Wi-Fi
  settings and the proxy URL.

## Device behavior

- Poll conservatively and preserve the last successful usage response.
- Honor rate-limit cooldowns and do not let touch refresh bypass them.
- Prefer USB deployment; do not add unauthenticated remote update mechanisms.
- Treat the provider usage endpoints as unstable internal interfaces.

# Contributing

Contributions that improve the usage display, provider parsing,
device reliability, documentation, or tests are welcome.

Before opening a pull request:

1. Keep credentials and captured provider responses out of the repository.
2. Run `devenv shell -- python -m unittest discover -s tests`.
3. Compile-check `projects/usage-dashboard/code.py` as documented in the README.
4. Run `git diff --check`.
5. Describe any physical-device validation you performed, using the loop in
   [docs/DEVICE.md](docs/DEVICE.md): `deploy usage-dashboard` then `verify`.

Keep unrelated PyPortal applications and general home-automation tooling in
separate repositories.

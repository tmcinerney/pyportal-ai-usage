# Contributing

Contributions that improve the Claude/Codex usage display, provider parsing,
device reliability, documentation, or tests are welcome.

Before opening a pull request:

1. Keep credentials and captured provider responses out of the repository.
2. Run `devenv shell -- python -m unittest discover -s tests`.
3. Compile-check `projects/usage-dashboard/code.py` as documented in the README.
4. Run `git diff --check`.
5. Describe any physical-device validation you performed.

Keep unrelated PyPortal applications and general home-automation tooling in
separate repositories.

# Security policy

## Credential model

Provider credentials stay on the computer running the proxy. The PyPortal only
stores its Wi-Fi credentials and the proxy URL. Proxy responses contain
normalized usage values, not tokens or raw provider responses.

`shared/secrets.py` is intentionally ignored. Do not commit it, a copied
`secrets.py`, Claude or Codex auth files, captured HTTP traffic, or diagnostic
output containing credentials.

## Network exposure

The proxy must be reachable by the PyPortal, so it listens on all interfaces by
default. Its local API is not authenticated. Run it only on a trusted network,
restrict port 9292 with a firewall, or set `BIND_HOST` to a more specific
interface address. Do not expose it directly to the internet.

## Upstream interfaces

The Claude and Codex usage endpoints used here are internal client interfaces,
not documented public APIs. Their authentication and payload formats may change.
Review upstream changes before updating parsing or credential discovery code.

## Reporting a vulnerability

Open a private security advisory in the GitHub repository. Do not include live
credentials, raw auth files, or token-bearing request captures in an issue.

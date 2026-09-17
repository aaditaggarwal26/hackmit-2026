# Bundled type

`archivo-latin.woff2` — Archivo v25, variable (weight 400–800, width 62–125%),
latin subset, 90 KB. Omnibus-Type, SIL Open Font License 1.1 (`OFL.txt`).
Source: Google Fonts, `fonts.gstatic.com/s/archivo/v25/k3kQo8UDI-1M0wlSfdnoLmvDIaI.woff2`.

Committed rather than linked so the dashboard renders identically with the
network off (`tools/offline_check.py` fails on any `https://` reference from
`index.html`). Served from `/static/fonts/` by `viz/server.py`'s StaticFiles
mount; the `@font-face` rule uses that absolute-path URL, never a scheme.

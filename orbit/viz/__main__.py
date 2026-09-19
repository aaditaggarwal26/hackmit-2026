"""`uv run python -m orbit.viz [--scenario conflict] [--source catalogue]`:
server + orchestrator on sim:// nodes in one command. The server lives in
viz/server.py next to its static files; this is just the entry point."""
from viz.server import main

raise SystemExit(main())

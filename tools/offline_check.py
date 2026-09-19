"""Prove the demo needs no network.

    uv run python tools/offline_check.py          # prints PASS/FAIL lines, exits non-zero on any FAIL

Three checks, each a function returning (ok, lines) so tests/test_offline_check.py
runs them in-process:

  check_static   walks the import graph of orbit.orchestrator, orbit.corpus and viz.server
                 (repo files only, lazy imports included) and fails on any import of, or
                 call into, an HTTP library — or socket.create_connection / socket.socket —
                 anywhere in it. The only network code in the repo, orbit/corpus/fetch.py::
                 fetch_live (the corpus build CLI), must not be in that graph at all.
  check_dynamic  swaps socket.socket / create_connection / getaddrinfo for guards that
                 raise for any non-loopback destination, then runs the orchestrator
                 headless on the committed corpus and drives the FastAPI app through
                 TestClient (/, /api/state, /corpus/0000.png, one WebSocket).
  check_assets   index.html references nothing off-box and every manifest frame has its
                 committed thumbnail under corpus/png.
"""
from __future__ import annotations

import ast
import contextlib
import io
import re
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("orbit/orchestrator/main.py", "orbit/orchestrator/scenarios.py", "orbit/corpus/__init__.py", "viz/server.py")
PACKAGES = ("orbit", "viz", "tools")
NET_MODULES = ("requests", "urllib.request", "urllib3", "http.client", "httpx", "aiohttp")
NET_CALLS = NET_MODULES + ("socket.create_connection", "socket.socket")
LIVE_PATH = REPO / "orbit" / "corpus" / "fetch.py"     # the ONLY file that may touch the network (corpus build CLI)
LIVE_FUNC = "fetch_live"
LIVE_DEFAULT_FALSE: dict = {}                            # fetch.py is a CLI, never imported by the demo (checked below)
INDEX = REPO / "viz" / "static" / "index.html"
CORPUS_PNG = REPO / "corpus" / "png"


# --- (a) static ---------------------------------------------------------------------------
def _module_file(name: str) -> Path | None:
    if name.split(".")[0] not in PACKAGES:
        return None
    base = REPO.joinpath(*name.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.exists():
            return p
    return None


def _imports(path: Path, tree: ast.AST) -> set[Path]:
    """Repo files this file imports, at module level or lazily inside functions."""
    pkg = ".".join(path.relative_to(REPO).with_suffix("").parts[:-1])   # package dir, for both x.py and __init__.py
    out: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if (f := _module_file(a.name)):
                    out.add(f)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = pkg.split(".")[: len(pkg.split(".")) - node.level + 1]
                mod = ".".join(parts + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if (f := _module_file(mod)):
                out.add(f)
            for a in node.names:                      # `from a.b import c` where c is a submodule
                if (f := _module_file(f"{mod}.{a.name}")):
                    out.add(f)
    return out


def import_graph(roots=ROOTS) -> dict[Path, ast.Module]:
    todo = [REPO / r for r in roots]
    seen: dict[Path, ast.Module] = {}
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        tree = ast.parse(p.read_text(), filename=str(p))
        seen[p] = tree
        todo.extend(f for f in _imports(p, tree) if f not in seen)
    return seen


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _enclosing_func(node: ast.AST, parents: dict) -> str | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _under_if_live(node: ast.AST, parents: dict) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "live":
            return True
    return False


def scan_file(path: Path, tree: ast.AST) -> tuple[list[str], list[str], int]:
    """(allowed, bad, fetch_live calls) for one file: HTTP-library imports/calls and socket
    connects are allowed only inside celestrak.py::fetch_live; fetch_live() only under `if live:`."""
    rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    parents = _parents(tree)
    allowed, bad, fetch_calls = [], [], 0
    net_roots = {m.split(".")[0] for m in NET_MODULES}
    for node in ast.walk(tree):
        hit = None
        if isinstance(node, ast.Import):
            hit = next((a.name for a in node.names if a.name.split(".")[0] in net_roots), None)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in net_roots:
                hit = node.module
        elif isinstance(node, ast.Call):
            name = _dotted(node.func) or ""
            if any(name == c or name.startswith(c + ".") for c in NET_CALLS):
                hit = name + "()"
            if name == LIVE_FUNC or name.endswith("." + LIVE_FUNC):
                fetch_calls += 1
                if path != LIVE_PATH or not _under_if_live(node, parents):
                    bad.append(f"{rel}:{node.lineno}: {LIVE_FUNC}() called outside `if live:`")
        if hit:
            func = _enclosing_func(node, parents)
            if path == LIVE_PATH and func == LIVE_FUNC:
                allowed.append(f"allowed: {rel}:{node.lineno} {hit} inside {LIVE_FUNC}() (the --live path)")
            else:
                bad.append(f"{rel}:{node.lineno}: {hit} in {func or '<module>'} is reachable without --live")
    return allowed, bad, fetch_calls


def check_static() -> tuple[bool, list[str]]:
    lines: list[str] = []
    graph = import_graph()
    lines.append(f"import graph from {', '.join(ROOTS)}: {len(graph)} repo files")
    bad: list[str] = []
    fetch_calls = 0
    for path, tree in sorted(graph.items()):
        a, b, n = scan_file(path, tree)
        lines += a
        bad += b
        fetch_calls += n
    if LIVE_PATH in graph:
        bad.append(f"{LIVE_PATH.relative_to(REPO)} is reachable from the demo import graph")
    else:
        lines.append(f"{LIVE_PATH.relative_to(REPO)} (the only network code) is not imported by the demo")
    a, b, n = scan_file(LIVE_PATH, ast.parse(LIVE_PATH.read_text()))
    lines += a
    if not a:
        bad.append(f"{LIVE_PATH.relative_to(REPO)}::{LIVE_FUNC} has no network call; the guard test is vacuous")
    bad += [x for x in b if "outside `if live:`" not in x]      # a CLI may call fetch_live unconditionally
    for path, funcs in LIVE_DEFAULT_FALSE.items():
        tree = graph.get(path) or ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in funcs:
                args = node.args
                names = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
                pos = [a.arg for a in args.args]
                d = None
                if "live" in pos:
                    i = pos.index("live") - (len(pos) - len(args.defaults))
                    d = args.defaults[i] if i >= 0 else None
                elif "live" in [a.arg for a in args.kwonlyargs]:
                    d = args.kw_defaults[[a.arg for a in args.kwonlyargs].index("live")]
                if "live" in names:
                    if isinstance(d, ast.Constant) and d.value is False:
                        lines.append(f"{path.relative_to(REPO)}::{node.name} live=False by default")
                    else:
                        bad.append(f"{path.relative_to(REPO)}::{node.name}: `live` does not default to False")
    lines.extend(bad)
    return not bad, lines


# --- (b) dynamic ------------------------------------------------------------------------------
class NetworkBlocked(RuntimeError):
    pass


def _is_loopback(addr) -> bool:
    if addr is None or isinstance(addr, (str, bytes)):     # AF_UNIX path, or getaddrinfo(None, port) for bind
        return True
    host = addr[0] if isinstance(addr, tuple) else addr
    if host is None or isinstance(host, bytes):
        return True
    host = str(host).strip("[]").lower()
    return host in ("", "localhost", "::1", "0.0.0.0", "::") or host.startswith("127.") or host.startswith("::ffff:127.")


def _guard(addr) -> None:
    if not _is_loopback(addr):
        raise NetworkBlocked(f"offline check: connection to {addr!r} refused (non-loopback)")


@contextlib.contextmanager
def no_network():
    """Inside: any socket connect / create_connection / DNS lookup to a non-loopback host raises."""
    real_socket, real_cc, real_gai = socket.socket, socket.create_connection, socket.getaddrinfo

    class GuardedSocket(real_socket):
        def connect(self, addr):
            _guard(addr)
            return super().connect(addr)

        def connect_ex(self, addr):
            _guard(addr)
            return super().connect_ex(addr)

        def sendto(self, *args):
            _guard(args[-1])
            return super().sendto(*args)

    def create_connection(address, *a, **k):
        _guard(address)
        return real_cc(address, *a, **k)

    def getaddrinfo(host, *a, **k):
        _guard((host,))
        return real_gai(host, *a, **k)

    socket.socket, socket.create_connection, socket.getaddrinfo = GuardedSocket, create_connection, getaddrinfo
    try:
        yield
    finally:
        socket.socket, socket.create_connection, socket.getaddrinfo = real_socket, real_cc, real_gai


def check_dynamic(segments: int = 2) -> tuple[bool, list[str]]:
    lines: list[str] = []
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    with no_network():
        # the guard is armed: prove it before trusting a PASS
        try:
            socket.create_connection(("gibs.earthdata.nasa.gov", 443), timeout=1)
        except NetworkBlocked as e:
            lines.append(f"guard armed: {e}")
        else:
            return False, lines + ["guard NOT armed: create_connection to gibs.earthdata.nasa.gov went through"]
        # headless orchestrator on the committed corpus
        from orbit.orchestrator.main import main as orch_main
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = orch_main(["--scenario", "nominal", "--passes", str(segments)])
        except NetworkBlocked as e:
            return False, lines + [f"orchestrator tried the network: {e}"]
        summary = buf.getvalue().strip().splitlines()
        lines += [f"orchestrator --scenario nominal --passes {segments}: exit {rc}"] + ["  " + s for s in summary[:3]]
        if rc != 0 or not any(s.startswith("scenario=") for s in summary):
            return False, lines + ["orchestrator did not print a summary"]
        # FastAPI app through TestClient: in-process ASGI, no listening socket
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:                       # httpx is a dev dependency
            return False, lines + [f"fastapi TestClient unavailable ({e}); `uv sync` installs the dev group"]
        from viz.server import create_app
        app = create_app("nominal", virtual=True, paused=True)
        try:
            with TestClient(app) as client:
                r = client.get("/")
                ok_index = r.status_code == 200 and "<title>" in r.text
                lines.append(f"GET / -> {r.status_code}, {'title found' if ok_index else 'no <title>'}")
                r = client.get("/api/state")
                s = r.json() if r.status_code == 200 else {}
                ok_state = r.status_code == 200 and s.get("type") == "snapshot" and s.get("scenario") == "nominal"
                lines.append(f"GET /api/state -> {r.status_code}, scenario={s.get('scenario')} paused={s.get('paused')} "
                             f"nodes={[n.get('path') for n in s.get('nodes', [])]}")
                r = client.get("/corpus/0000.png")
                ok_vendor = r.status_code == 200 and r.content[:4] == b"\x89PNG"
                lines.append(f"GET /corpus/0000.png -> {r.status_code}, {len(r.content)} bytes")
                with client.websocket_connect("/ws") as ws:
                    m = ws.receive_json()
                ok_ws = m.get("type") == "snapshot" and m.get("scenario") == "nominal"
                lines.append(f"WS /ws -> first message type={m.get('type')} scenario={m.get('scenario')}")
        except NetworkBlocked as e:
            return False, lines + [f"server tried the network: {e}"]
        ok = ok_index and ok_state and ok_vendor and ok_ws
    return ok, lines


# --- (c) page assets --------------------------------------------------------------------------
def check_assets() -> tuple[bool, list[str]]:
    """index.html must reference nothing off-box (no http(s):// in src/href/@import/url()) and
    the corpus thumbnails it shows must be committed."""
    lines: list[str] = []
    ok = True
    html = INDEX.read_text() if INDEX.exists() else ""
    ext = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', html) + re.findall(r'url\(\s*["\']?(https?://[^)"\']+)', html) \
        + re.findall(r'@import\s+["\'](https?://[^"\']+)', html)
    if html and not ext:
        lines.append("index.html: no external src/href/url()/@import")
    else:
        ok = False
        lines.append(f"index.html: external references {ext or '(file missing)'}")
    pngs = sorted(CORPUS_PNG.glob("*.png")) if CORPUS_PNG.exists() else []
    import json
    n = len(json.loads((REPO / "corpus" / "manifest.json").read_text())["frames"])
    if len(pngs) == n and n > 0:
        lines.append(f"corpus/png: {n} thumbnails, one per manifest frame")
    else:
        ok = False
        lines.append(f"corpus/png: {len(pngs)} thumbnails for {n} manifest frames")
    return ok, lines


CHECKS = (("static", check_static), ("dynamic", check_dynamic), ("assets", check_assets))


def main(argv=None) -> int:
    failed = False
    for name, fn in CHECKS:
        ok, lines = fn()
        failed |= not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        for line in lines:
            print(f"       {line}")
    print("OFFLINE CHECK:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

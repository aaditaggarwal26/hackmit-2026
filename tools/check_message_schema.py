"""Fail if the firmware emits a message body the ground station cannot decode.

orbit/protocol/messages.py is the wire contract: ``from_doc`` walks ``fields(cls)`` and
rejects the datagram the moment one is absent. The firmware builds the same documents by
hand with ArduinoJson, so a field that is declared on one side and never set on the other
is invisible until a satellite is on the bench and every one of its frames is counted as
malformed. That is the failure this file exists to find at build time instead.

Neither side is transcribed here. The firmware's field set is parsed out of the .ino and
its headers -- following the helpers, so ``fillEnvelope``/``addBufferStats`` count as the
fields they actually write -- and the ground's is read off the dataclasses by
introspection, with REQUIRED vs OPTIONAL measured by deleting each field from the type's
own example vector and asking ``decode()`` whether it still accepts it.

  MISSING  the ground requires it, the firmware never sets it   -> error, exit 1
  EXTRA    the firmware sets it, the ground declares no field   -> warning, exit 0

EXTRA is only a warning because ``from_doc`` ignores keys it does not know; MISSING is
always fatal because the decoder always rejects.

  uv run python tools/check_message_schema.py
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import re
import sys
import types
from collections import defaultdict
from dataclasses import MISSING, dataclass, fields, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Union, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = ROOT / "firmware/satellite_esp32"
MAX_INLINE_DEPTH = 8

# --- firmware: what the .ino actually writes -------------------------------------------

# A function definition whose return type starts the line. Members of a class or a
# statement's `if (...) {` are indented, so the column-0 anchor is the whole filter.
_FUNC = re.compile(r"^[A-Za-z_][\w:*&<>\s]*?\b(\w+)\s*\(([^;{}()\n]*)\)\s*\{", re.M)
# doc["a"]["b"] = ...  -- a WRITE. Every read in the firmware is `doc["a"] | default`
# or `doc["a"].as<T>()`, so requiring `] =` (and not `==`) keeps reads out entirely.
_WRITE = re.compile(r"\b(\w+)((?:\[\s*\"\w+\"\s*\])+)\s*=(?!=)([^;]*);")
_ALIAS = re.compile(r"\bJson(?:Object|Array|Variant)\s*&?\s*(\w+)\s*=\s*([^;]+);")
_DECL = re.compile(r"\bJsonDocument\s+(\w+)\s*;")
_CALL = re.compile(r"\b(\w+)\s*\(")
_KEY = re.compile(r"\[\s*\"(\w+)\"\s*\]")
_SUFFIX_TO = re.compile(r"\.\s*(?:to|as)\s*<[^>]*>\s*\(\s*\)\s*$")
_SUFFIX_ADD = re.compile(r"\.\s*add\s*<[^>]*>\s*\(\s*\)\s*$")
_LITERAL = re.compile(r'\s*"([^"]*)"\s*\Z')
# `#define TYPE_FAULT "fault"` / `static const char *TYPE_FAULT = "fault";` -- a type name
# reached through a constant must still resolve, or the type looks like it is never emitted.
_CONST = re.compile(r'#define\s+(\w+)\s+"([^"]*)"|const\s+char\s*\*\s*(\w+)\s*=\s*"([^"]*)"')
_NOT_A_CALL = frozenset({"if", "for", "while", "switch", "return", "sizeof", "delay"})


@dataclass(frozen=True)
class _Func:
    name: str
    params: tuple[tuple[str, str], ...]  # (declared type, parameter name)
    body: str


def _strip_comments(text: str) -> str:
    """Blank out // and /* */, leaving string literals (the message type names) intact."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = _end_of_literal(text, i)
            out.append(text[i:j])
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _end_of_literal(text: str, i: int) -> int:
    quote, j, n = text[i], i + 1, len(text)
    while j < n and text[j] != quote:
        j += 2 if text[j] == "\\" else 1
    return min(j + 1, n)


def _balanced(text: str, i: int, open_ch: str, close_ch: str) -> int:
    """Index just past the delimiter matching the one at `i`, skipping literals."""
    depth, j, n = 0, i, len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = _end_of_literal(text, j)
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            j = _end_of_literal(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur))
    return [a.strip() for a in out]


def _params_of(decl: str) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for raw in _split_args(decl):
        p = raw.split("=")[0].strip()
        if not p or p == "void":
            continue
        m = re.search(r"(\w+)\s*\Z", p)
        if m:
            out.append((p[: m.start(1)], m.group(1)))
    return tuple(out)


def _functions(text: str) -> dict[str, _Func]:
    src = _strip_comments(text)
    out: dict[str, _Func] = {}
    for m in _FUNC.finditer(src):
        brace = src.index("{", m.end() - 1)
        end = _balanced(src, brace, "{", "}")
        out[m.group(1)] = _Func(m.group(1), _params_of(m.group(2)), src[brace + 1 : end - 1])
    return out


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _resolve(expr: str, binds: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """`d`, `d["buffer"].to<JsonObject>()`, `w.add<JsonObject>()` -> (document id, path prefix)."""
    e, element = expr.strip(), False
    while True:
        stripped = _SUFFIX_TO.sub("", e).strip()
        if stripped != e:
            e = stripped
            continue
        stripped = _SUFFIX_ADD.sub("", e).strip()
        if stripped != e:
            e, element = stripped, True
            continue
        break
    e = e.lstrip("&*").strip()
    m = re.fullmatch(r"(\w+)((?:\[\s*\"\w+\"\s*\])*)", e)
    if m is None or m.group(1) not in binds:
        return None
    root, prefix = binds[m.group(1)]
    for key in _KEY.findall(m.group(2)):
        prefix = _join(prefix, key)
    return (root, prefix + "[]" if element else prefix)


def _substitute(text: str, subs: dict[str, str]) -> str:
    if not subs:
        return text
    return re.sub(r"\b\w+\b", lambda m: subs.get(m.group(0), m.group(0)), text)


def _events(body: str, known: frozenset[str]) -> list[tuple[int, str, tuple[Any, ...]]]:
    """Every declaration, alias, write and helper call in the body, in source order."""
    out: list[tuple[int, str, tuple[Any, ...]]] = []
    for m in _DECL.finditer(body):
        out.append((m.start(), "decl", (m.group(1),)))
    for m in _ALIAS.finditer(body):
        out.append((m.start(), "alias", (m.group(1), m.group(2))))
    for m in _WRITE.finditer(body):
        out.append((m.start(), "write", (m.group(1), tuple(_KEY.findall(m.group(2))), m.group(3))))
    for m in _CALL.finditer(body):
        name = m.group(1)
        if name not in known or name in _NOT_A_CALL:
            continue
        paren = body.index("(", m.end() - 1)
        end = _balanced(body, paren, "(", ")")
        if body[end:].lstrip()[:1] != ";":  # a statement call, not a subexpression
            continue
        out.append((m.start(), "call", (name, _split_args(body[paren + 1 : end - 1]))))
    out.sort(key=lambda e: e[0])
    return out


def _writes(
    fn: _Func,
    table: dict[str, _Func],
    bound: dict[str, tuple[str, str]],
    subs: dict[str, str],
    uid: str,
    stack: frozenset[str],
) -> list[tuple[str, str, str]]:
    """(document id, dotted field path, right-hand side) for every field this function sets.

    `bound` carries the caller's documents in for JSON-typed parameters, which is what
    makes a helper like addBufferStats(d["buffer"].to<JsonObject>()) count as buffer.*
    on the caller's document rather than as a message of its own.
    """
    out: list[tuple[str, str, str]] = []
    binds = dict(bound)
    known = frozenset(table)
    for pos, kind, payload in _events(fn.body, known):
        if kind == "decl":
            binds[payload[0]] = (f"{uid}/{payload[0]}", "")
        elif kind == "alias":
            target = _resolve(payload[1], binds)
            if target is None:
                binds.pop(payload[0], None)
            else:
                binds[payload[0]] = target
        elif kind == "write":
            var, keys, rhs = payload
            if var not in binds:
                continue  # an unbound local or an incoming document: not something we emit
            root, prefix = binds[var]
            for key in keys:
                prefix = _join(prefix, key)
            out.append((root, prefix, _substitute(rhs, subs)))
        elif kind == "call":
            name, args = payload
            callee = table[name]
            if name in stack or len(stack) >= MAX_INLINE_DEPTH:
                continue
            sub_bound: dict[str, tuple[str, str]] = {}
            sub_subs: dict[str, str] = {}
            for i, (ptype, pname) in enumerate(callee.params):
                arg = args[i] if i < len(args) else ""
                if "Json" in ptype:
                    target = _resolve(arg, binds)
                    if target is not None:
                        sub_bound[pname] = target
                else:
                    sub_subs[pname] = _substitute(arg, subs)
            out.extend(_writes(callee, table, sub_bound, sub_subs, f"{uid}>{name}@{pos}", stack | {name}))
    return out


def firmware_fields(*sources: str) -> dict[str, set[str]]:
    """message type -> the JSON field paths the firmware sets on it.

    A document is a message only once something writes a string literal to its ``type``,
    which is what attributes each field to the right type without a hardcoded list.
    """
    table: dict[str, _Func] = {}
    consts: dict[str, str] = {}
    for text in sources:
        stripped = _strip_comments(text)
        table.update(_functions(text))
        for c in _CONST.finditer(stripped):
            consts[c.group(1) or c.group(3)] = c.group(2) if c.group(1) else c.group(4)
    out: dict[str, set[str]] = {}
    for name, fn in table.items():
        docs: defaultdict[str, dict[str, str]] = defaultdict(dict)
        for root, path, rhs in _writes(fn, table, {}, {}, name, frozenset({name})):
            docs[root][path] = rhs
        for paths in docs.values():
            rhs = paths.get("type", "")
            m = _LITERAL.match(rhs)
            mtype = m.group(1) if m else consts.get(rhs.strip())
            if mtype is None:
                continue
            out.setdefault(mtype, set()).update(paths)
    return out


# --- ground: what orbit/protocol/messages.py declares -----------------------------------


def _expand(path: str, typ: Any) -> list[str]:
    """A declared field -> its leaf paths. ``parts`` -> parts.clear/sharp/change."""
    if get_origin(typ) in (types.UnionType, Union):
        # An optional field is still whatever it is when present: `X | None` expands as X.
        held = [a for a in get_args(typ) if a is not type(None)]
        return _expand(path, held[0]) if len(held) == 1 else [path]
    if get_origin(typ) is tuple:
        args = get_args(typ)
        inner = args[0] if args else None
        if isinstance(inner, type) and is_dataclass(inner):
            hints = get_type_hints(inner)
            return [p for f in fields(inner) for p in _expand(f"{path}[].{f.name}", hints[f.name])]
        return [f"{path}[]"]
    if isinstance(typ, type) and is_dataclass(typ):
        hints = get_type_hints(typ)
        return [p for f in fields(typ) for p in _expand(f"{path}.{f.name}", hints[f.name])]
    return [path]


def _drop(doc: Any, parts: list[str]) -> bool:
    """Remove a leaf from a decoded example. False when there was nothing to remove."""
    head, rest = parts[0], parts[1:]
    if not isinstance(doc, dict):
        return False
    if head.endswith("[]"):
        key = head[:-2]
        seq = doc.get(key)
        if not isinstance(seq, list) or not seq:
            return False
        if not rest:
            del doc[key]
            return True
        return all([_drop(e, rest) for e in seq])
    if head not in doc:
        return False
    if rest:
        return _drop(doc[head], rest)
    del doc[head]
    return True


def _has_default(f: Any) -> bool:
    return f.default is not MISSING or f.default_factory is not MISSING


def ground_fields(mod: ModuleType) -> tuple[dict[str, set[str]], dict[str, set[str]], frozenset[str]]:
    """(required, optional, envelope keys) per satellite message type, by introspection.

    Optionality is measured, not assumed: each leaf is deleted from the type's own example
    vector and ``decode()`` is asked whether the datagram still round-trips. That reads
    whatever the ground currently does -- a dataclass default, a union with None, nothing
    at all -- instead of guessing at the mechanism. Types with no example fall back to
    "a field with a default is optional".
    """
    base = {f.name for f in fields(mod.Message)}
    examples = dict(mod.examples())
    required: dict[str, set[str]] = {}
    optional: dict[str, set[str]] = {}
    envelope: set[str] = set()

    for mtype in sorted(mod.SATELLITE_TYPES):
        name = str(mtype)
        cls = mod.MESSAGE_TYPES[mtype]
        hints = get_type_hints(cls)
        body = [f for f in fields(cls) if f.name not in base]
        leaves = {p: f for f in body for p in _expand(f.name, hints[f.name])}
        msg = examples.get(name)
        doc: dict[str, Any] | None = None
        if msg is not None:
            doc = json.loads(msg.encode())
            envelope |= set(doc) - {f.name for f in body}

        opt: set[str] = set()
        for leaf, field in leaves.items():
            if doc is not None:
                probe = copy.deepcopy(doc)
                if _drop(probe, leaf.split(".")):
                    if not isinstance(mod.decode(json.dumps(probe).encode()), mod.DecodeError):
                        opt.add(leaf)
                    continue
            if _has_default(field):
                opt.add(leaf)
        required[name] = set(leaves) - opt
        optional[name] = opt
    return required, optional, frozenset(envelope)


# --- the diff ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeDiff:
    name: str
    emitted: bool
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    envelope_missing: tuple[str, ...]


def diff(
    firmware: dict[str, set[str]],
    required: dict[str, set[str]],
    optional: dict[str, set[str]] | None = None,
    envelope: frozenset[str] = frozenset(),
) -> list[TypeDiff]:
    """Per type: what the ground requires and never gets, and what it is sent and ignores."""
    optional = optional or {}
    out: list[TypeDiff] = []
    for name in sorted(required):
        emitted = name in firmware
        sets = firmware.get(name, set())
        body = sets - envelope
        known = required[name] | optional.get(name, set())
        out.append(
            TypeDiff(
                name=name,
                emitted=emitted,
                missing=tuple(sorted(required[name] - body)),
                extra=tuple(sorted(body - known)),
                envelope_missing=tuple(sorted(envelope - sets)) if emitted else (),
            )
        )
    return out


def report(diffs: list[TypeDiff]) -> int:
    bad = [d for d in diffs if d.missing or d.envelope_missing]
    warn = [d for d in diffs if d.extra]

    for d in warn:
        print(f"warning: {d.name}: the firmware sets {', '.join(d.extra)}; the ground declares no such field")

    if not bad:
        names = ", ".join(d.name for d in diffs)
        print(f"OK: {len(diffs)} satellite message types match orbit/protocol/messages.py ({names})")
        return 0

    print("the firmware does not set every field the ground station requires:\n")
    total = 0
    for d in bad:
        if not d.emitted:
            print(f"  {d.name}: NEVER EMITTED by the firmware")
        else:
            print(f"  {d.name}:")
        for f in d.envelope_missing:
            print(f"      envelope {f}: never set")
            total += 1
        for f in d.missing:
            print(f"      MISSING {f}")
            total += 1
    print(f"\n{total} required fields are never set, across {len(bad)} of {len(diffs)} satellite message types")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--firmware", type=Path, default=FIRMWARE_DIR, help="satellite sketch directory")
    a = ap.parse_args()

    sketches = sorted(a.firmware.glob("*.ino")) + sorted(a.firmware.glob("*.h"))
    if not sketches:
        print(f"no firmware sources under {a.firmware}", file=sys.stderr)
        return 2
    try:
        mod = importlib.import_module("orbit.protocol.messages")
    except Exception as e:  # mid-edit, or a dependency missing: say so, do not claim a drift
        print(f"cannot read the ground contract: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    fw = firmware_fields(*(p.read_text() for p in sketches))
    required, optional, envelope = ground_fields(mod)
    ground_only = {str(t) for t in mod.GROUND_TYPES}
    for name in sorted(set(fw) - set(required)):
        why = "a ground → satellite type" if name in ground_only else "not a type the ground knows"
        print(f"warning: the firmware emits {name!r}, which is {why}")
    return report(diff(fw, required, optional, envelope))


if __name__ == "__main__":
    raise SystemExit(main())

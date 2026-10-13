"""Cassettes: recorded HTTP request/response pairs, one JSON file each, service-independent.

Layout: <dir>/<route>/<sha256 of the request key>.json,
where a file is
  {"request": {"method", "path", "query", "host", "body"}, "status", "content_type", "body"}
and the key is method + path + sorted query + (host, when the route keys on it) + body. Written by
httpcassette.server in record mode; read here to list, show and compare sets. Uses: replaying a service's
dependencies, load tests (replay realistic traffic or answers), comparing two environments' answers to the same calls.

Stdlib only.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class Entry:
    route: str
    name: str  # file name: the key's sha256 + .json
    path: Path
    method: str
    req_path: str
    query: str
    host: str
    body: str
    status: int
    content_type: str
    response: str
    mtime: float

    @property
    def rel(self) -> str:
        return f"{self.route}/{self.name}"


def load(path: Path, route: str) -> Optional[Entry]:
    try:
        d = json.loads(path.read_text())
        r = d.get("request") or {}
        return Entry(route, path.name, path, r.get("method", ""), r.get("path", ""), r.get("query", ""), r.get("host", ""),
                     r.get("body", ""), int(d.get("status", 0)), d.get("content_type", ""), d.get("body", ""), path.stat().st_mtime)
    except (OSError, ValueError, TypeError):
        return None


def entries(root: Path, route: Optional[str] = None) -> Iterator[Entry]:
    """Every cassette under root (one level of route directories), unreadable files skipped."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"no cassette dir {root}")
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if route and d.name != route:
            continue
        for f in sorted(d.glob("*.json")):
            e = load(f, d.name)
            if e:
                yield e


def path_shape(p: str, id_prefixes: tuple = ()) -> str:
    """A path with its variable segments blanked, for grouping: /api/v0/sites/site-123456/dimensions ->
    /api/v0/sites/*/dimensions. Variable = has digits and is longer than 6, is longer than 40, or starts with one
    of id_prefixes."""
    out = []
    for seg in p.split("/"):
        if seg and (any(ch.isdigit() for ch in seg) and len(seg) > 6 or (id_prefixes and seg.startswith(id_prefixes)) or len(seg) > 40):
            out.append("*")
        else:
            out.append(seg)
    return "/".join(out)


def summary(root: Path, id_prefixes: tuple = ()) -> dict:
    """Per route: count, bytes, status codes, first/last recorded, most common path shapes (path_shape)."""
    routes: dict[str, dict] = {}
    for e in entries(root):
        r = routes.setdefault(e.route, {"count": 0, "bytes": 0, "status": Counter(), "first": e.mtime, "last": e.mtime, "paths": Counter()})
        r["count"] += 1
        r["bytes"] += len(e.response) + len(e.body)
        r["status"][e.status] += 1
        r["first"], r["last"] = min(r["first"], e.mtime), max(r["last"], e.mtime)
        r["paths"][f"{e.method} {path_shape(e.req_path, id_prefixes)}"] += 1
    return {name: {**r, "status": dict(r["status"]), "paths": r["paths"].most_common(5)} for name, r in sorted(routes.items())}


def _same_body(a: str, b: str) -> bool:
    """Response bodies equal as JSON when both parse (key order ignored), else as text."""
    if a == b:
        return True
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return False


def diff(a: Path, b: Path) -> dict:
    """Compare two sets by request key: same, differs (status or body), only_a, only_b — lists of Entry pairs/entries."""
    ea = {e.rel: e for e in entries(a)}
    eb = {e.rel: e for e in entries(b)}
    out: dict[str, list] = defaultdict(list)
    for rel in sorted(set(ea) | set(eb)):
        x, y = ea.get(rel), eb.get(rel)
        if x and y:
            out["same" if x.status == y.status and _same_body(x.response, y.response) else "differs"].append((x, y))
        elif x:
            out["only_a"].append(x)
        else:
            out["only_b"].append(y)
    return dict(out)


def body_diff(a: str, b: str, limit: int = 6) -> list[str]:
    """The top-level JSON paths where two response bodies differ (a few), for a readable diff line."""
    try:
        ja, jb = json.loads(a), json.loads(b)
    except ValueError:
        return ["(not JSON: bodies differ as text)"]
    found: list[str] = []

    def walk(x, y, p):
        if len(found) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                if k not in x or k not in y:
                    found.append(f"{p}/{k}: {'only in b' if k not in x else 'only in a'}")
                else:
                    walk(x[k], y[k], f"{p}/{k}")
        elif x != y:
            found.append(f"{p}: {json.dumps(x)[:60]} vs {json.dumps(y)[:60]}")

    walk(ja, jb, "")
    return found or ["(equal as JSON)"]


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

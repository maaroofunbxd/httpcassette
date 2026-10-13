#!/usr/bin/env python3
"""Record/replay proxy for any HTTP service: answers from cassettes, records new ones, or stubs.

Keep it one file, stdlib + requests (imported only to forward, so replay and stub need the standard library
alone), with no imports even from the rest of httpcassette: it is copied next to a preset file into an image, or
onto a volume for a sidecar, and run as `python server.py` (installed: `python -m httpcassette.server`).

What it stands in for comes in as data, a preset (a JSON object; `none` = no routes, every stub `{}`):
  routes          [[name, [path prefix, ...]], ...], first match wins
  default_route   the route of a path matching no prefix ("default")
  host_keyed      routes whose Host header is part of the key (calls that share a path and differ by Host)
  host_suffix     ... only a Host ending with this ("" = any); that Host is also forwarded upstream
  no_record       [[route, rule], ...] calls never written to a cassette (a login): rule = a path prefix or
                  substring, "=/" (GET of / only) or "body:<substring>"; replay answers them 200 with
  no_record_cookie  this Set-Cookie (a fixed session), record forwards them without storing
  stubs           [{"route", "path_contains"?, "status"?, "body"}, ...] stub answers, first match wins (else
                  200 {}); a string "{query.NAME}" anywhere in body becomes that query parameter's value
Key = method + path + sorted query + (Host, for host-keyed routes) + request body. Cassettes:
<dir>/<route>/<sha256 of the key>.json, one per request (request summary + status, content type, body), so a
directory of them diffs and merges like source. httpcassette.store reads, lists and compares them.

Modes (REPLAY_MODE): replay (a miss is a 404 carrying the key) | record (forward, store answers < 500) |
auto (replay, a miss is forwarded and recorded) | stub (no recordings needed: every call gets its route's stub).
REPLAY_MISS=stub: replay, but a miss gets the stub instead of a 404.

Environment: REPLAY_DIR (/cassettes), REPLAY_MODE, REPLAY_MISS, PORT (8080), REPLAY_QUIET=1, REPLAY_ROUTES
(`name=/pfx1,/pfx2=http://upstream;other=/=http://u2`, prefix / = the default route), UPSTREAM_<ROUTE> (a
route's upstream), REPLAY_PRESET (none | a NAME, read from NAME.json next to this file | a .json path) and
REPLAY_PRESET_JSON (the preset itself; wins over REPLAY_PRESET, for when this file travels alone).
Upstream proxies: ALL_PROXY / HTTP_PROXY (socks5h:// works with requests[socks]).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass
from dataclasses import field as _field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qsl, urlencode, urlsplit


@dataclass
class Config:
    """What the proxy knows about the service it stands in for (a preset, plus REPLAY_ROUTES)."""
    routes: list = _field(default_factory=list)  # [(name, (prefix, ...)), ...], first match wins
    default_route: str = "default"  # a path matching no prefix
    host_keyed: tuple = ()  # routes whose Host header (ending host_suffix) is part of the key
    host_suffix: str = ""
    no_record: tuple = ()  # (route, path prefix | "=/" | "body:<substring>") never written, answered with no_record_cookie
    no_record_cookie: str = ""
    stubs: tuple = ()  # ({"route", "path_contains"?, "status"?, "body"}, ...), first match wins; else 200 {}


def preset_config(preset: Optional[dict]) -> Config:
    """A preset (the JSON object described above; None or {} = none) as a Config."""
    p = preset or {}
    unknown = set(p) - {"name", "routes", "default_route", "host_keyed", "host_suffix", "no_record", "no_record_cookie", "stubs"}
    if unknown:
        raise ValueError(f"preset: unknown keys {sorted(unknown)}")
    return Config(routes=[(r[0], tuple(r[1])) for r in p.get("routes", [])], default_route=p.get("default_route", "default"),
                  host_keyed=tuple(p.get("host_keyed", ())), host_suffix=p.get("host_suffix", ""),
                  no_record=tuple(tuple(x) for x in p.get("no_record", ())), no_record_cookie=p.get("no_record_cookie", ""),
                  stubs=tuple(p.get("stubs", ())))


def load_preset(spec: str = "", inline: str = "") -> Optional[dict]:
    """REPLAY_PRESET_JSON (inline) or REPLAY_PRESET (spec): none | NAME (NAME.json next to this file) | a .json path."""
    if inline:
        return json.loads(inline)
    if not spec or spec == "none":
        return None
    path = Path(spec) if spec.endswith(".json") or "/" in spec else Path(__file__).resolve().parent / f"{spec}.json"
    if not path.is_file():
        raise ValueError(f"REPLAY_PRESET {spec!r}: no preset file {path} (none | NAME | FILE.json, or REPLAY_PRESET_JSON)")
    return json.loads(path.read_text())


CONFIG = Config()  # configure() / main() replace it


def parse_routes(spec: str) -> list[tuple[str, tuple, str]]:
    """'name=/pfx1,/pfx2=http://upstream;other=/=http://u2' -> [(name, prefixes, upstream)]."""
    out = []
    for part in filter(None, (x.strip() for x in spec.split(";"))):
        bits = part.split("=", 2)
        if len(bits) != 3 or not bits[0] or not bits[1]:
            raise ValueError(f"route {part!r}: want NAME=PREFIX[,PREFIX...]=UPSTREAM")
        out.append((bits[0], tuple(p for p in bits[1].split(",") if p), bits[2]))
    return out


_lock = threading.Lock()
_shared = None


def session():
    """One requests.Session shared by every request thread (ThreadingHTTPServer starts a thread per request, so a
    per-thread session would never be reused), with a pool big enough for a client's parallel workers: through a
    SOCKS tunnel a new connection per call can cost a fresh tunnel stream, and many at once draw 502s."""
    global _shared
    import requests

    with _lock:
        if _shared is None:
            s = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32, pool_block=True)
            s.mount("http://", adapter)
            s.mount("https://", adapter)
            _shared = s
    return _shared


def route_of(path: str) -> str:
    for name, prefixes in CONFIG.routes:
        if path.startswith(prefixes):
            return name
    return CONFIG.default_route


def is_login(method: str, path: str, body: bytes) -> bool:
    """A call that must never be written to a cassette (the preset's no_record rules, e.g. a login form carrying a
    password). Answered with no_record_cookie in replay; forwarded but not stored in record."""
    route = route_of(path)
    for r, rule in CONFIG.no_record:
        if r != route:
            continue
        if rule.startswith("body:") and rule[5:].encode() in body:
            return True
        if rule == "=/" and method == "GET" and path in ("", "/"):
            return True
        if not rule.startswith(("body:", "=")) and (path.startswith(rule) or rule in path):
            return True
    return False


def _keyed_host(route: str, host: str) -> bool:
    return route in CONFIG.host_keyed and bool(host) and host.endswith(CONFIG.host_suffix)


def request_key(method: str, path_qs: str, body: bytes, host: str = "") -> str:
    """method + path + sorted query + (the Host, for host-keyed routes) + body: what makes two calls the same call."""
    parts = urlsplit(path_qs)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    host_part = host.lower() if _keyed_host(route_of(parts.path), host.lower()) else ""
    return "\n".join([method.upper(), parts.path, query, host_part, body.decode("utf-8", "replace")])


def cassette_path(root: Path, key: str) -> Path:
    route = route_of(key.split("\n", 2)[1])
    return root / route / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def _fill(body, params: dict):
    """The stub body with every "{query.NAME}" string replaced by that query parameter's value."""
    if isinstance(body, dict):
        return {k: _fill(v, params) for k, v in body.items()}
    if isinstance(body, list):
        return [_fill(v, params) for v in body]
    if isinstance(body, str) and body.startswith("{query.") and body.endswith("}"):
        return params.get(body[7:-1], "")
    return body


def stub_answer(path: str, query: str) -> tuple[int, Union[dict, list]]:
    """The empty-but-valid answer of a path's route: the preset's first matching stub, else 200 {}."""
    route = route_of(path)
    for s in CONFIG.stubs:
        if s.get("route", route) == route and s.get("path_contains", "") in path:
            return int(s.get("status", 200)), _fill(s.get("body", {}), dict(parse_qsl(query)))
    return 200, {}


class Replay:
    def __init__(self, root: Path, mode: str = "replay", miss: str = "404", upstreams: Optional[dict] = None):
        if mode not in ("replay", "record", "auto", "stub"):
            raise ValueError("REPLAY_MODE is replay | record | auto | stub")
        if miss not in ("404", "stub"):
            raise ValueError("REPLAY_MISS is 404 | stub")
        self.root, self.mode, self.miss = root, mode, miss
        self.upstreams = upstreams or {}
        self.stats = {"hit": 0, "miss": 0, "recorded": 0, "stub": 0, "login": 0, "upstream_error": 0}

    def forward(self, method: str, path_qs: str, headers: dict, body: bytes) -> tuple[int, str, bytes, dict]:
        route = route_of(urlsplit(path_qs).path)
        base = self.upstreams.get(route)
        if not base:
            raise RuntimeError(f"no upstream for {route} (UPSTREAM_{route.upper()})")
        fwd = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        if _keyed_host(route, headers.get("Host", "")):
            fwd["Host"] = headers["Host"]  # host-routed upstreams (istio) pick the service by Host
        r = None
        for attempt in range(4):  # an upstream blip through the tunnel (reset, 502/503/504) is retried with backoff
            try:
                r = session().request(method, base.rstrip("/") + path_qs, headers=fwd, data=body or None, timeout=30, allow_redirects=False)
                if r.status_code not in (502, 503, 504):
                    break
            except Exception:  # noqa: BLE001 - connection errors through the tunnel
                if attempt == 3:
                    raise
            import time
            time.sleep(0.5 * 2 ** attempt)
        extra = {k: v for k, v in r.headers.items() if k.lower() in ("set-cookie", "location")}
        return r.status_code, r.headers.get("Content-Type", "application/json"), r.content, extra

    def handle(self, method: str, path_qs: str, headers: dict, body: bytes) -> tuple[int, str, bytes, dict]:
        parts = urlsplit(path_qs)
        if is_login(method, parts.path, body):
            with _lock:
                self.stats["login"] += 1
            if self.mode in ("record", "auto") and self.upstreams.get(route_of(parts.path)):
                try:
                    return self.forward(method, path_qs, headers, body)  # never written to a cassette
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"login forward failed: {exc}\n")
            return 200, "text/html", b"ok", ({"Set-Cookie": f"{CONFIG.no_record_cookie}; Path=/"} if CONFIG.no_record_cookie else {})
        if self.mode == "stub":
            return self._stub(parts)
        key = request_key(method, path_qs, body, headers.get("Host", ""))
        path = cassette_path(self.root, key)
        if self.mode in ("replay", "auto") and path.is_file():
            c = json.loads(path.read_text())
            with _lock:
                self.stats["hit"] += 1
            return c["status"], c.get("content_type", "application/json"), c["body"].encode(), {}
        if self.mode in ("record", "auto"):
            try:
                status, ctype, data, extra = self.forward(method, path_qs, headers, body)
            except Exception as exc:  # noqa: BLE001 - an unreachable upstream is reported, not hidden
                with _lock:
                    self.stats["upstream_error"] += 1
                return 502, "application/json", json.dumps({"error": f"upstream: {exc}", "key": key}).encode(), {}
            if status < 500:
                path.parent.mkdir(parents=True, exist_ok=True)
                req = {"method": method, "path": parts.path, "query": parts.query, "host": key.split("\n")[3], "body": body.decode("utf-8", "replace")}
                path.write_text(json.dumps({"request": req, "status": status, "content_type": ctype,
                                            "body": data.decode("utf-8", "replace")}, indent=1, sort_keys=True))
                with _lock:
                    self.stats["recorded"] += 1
            return status, ctype, data, extra
        with _lock:
            self.stats["miss"] += 1
        if self.miss == "stub":
            return self._stub(parts)
        sys.stderr.write(f"MISS {method} {path_qs}\n")
        return 404, "application/json", json.dumps({"error": "no recording for this request", "key": key}).encode(), {}

    def _stub(self, parts) -> tuple[int, str, bytes, dict]:
        with _lock:
            self.stats["stub"] += 1
        status, payload = stub_answer(parts.path, parts.query)
        return status, "application/json", json.dumps(payload).encode(), {}


def make_handler(replay: Replay):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if self.path == "/__replay/stats":
                status, ctype, data, extra = 200, "application/json", json.dumps({"mode": replay.mode, "miss": replay.miss, **replay.stats}).encode(), {}
            else:
                status, ctype, data, extra = replay.handle(self.command, self.path, dict(self.headers.items()), body)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, fmt, *args):  # one line per request on stderr, short
            if os.environ.get("REPLAY_QUIET") != "1":
                sys.stderr.write("%s %s\n" % (self.command, self.path[:160]))

    return Handler


def configure(preset: Optional[dict] = None, routes: str = "", upstreams: Optional[dict] = None) -> dict:
    """Set CONFIG from a preset (None = none) plus REPLAY_ROUTES-style routes, which replace the preset's routes;
    returns {route: upstream}."""
    global CONFIG
    base = preset_config(preset)
    ups = dict(upstreams or {})
    if routes:
        parsed = parse_routes(routes)
        CONFIG = Config(routes=[(n, pf) for n, pf, _ in parsed if pf != ("/",)],
                        default_route=next((n for n, pf, _ in parsed if pf == ("/",)), base.default_route),
                        host_keyed=base.host_keyed, host_suffix=base.host_suffix, no_record=base.no_record,
                        no_record_cookie=base.no_record_cookie, stubs=base.stubs)
        ups.update({n: u for n, _, u in parsed})
    else:
        CONFIG = base
    return ups


def route_names() -> list[str]:
    return [n for n, _ in CONFIG.routes] + [CONFIG.default_route]


def main() -> None:
    root = Path(os.environ.get("REPLAY_DIR", "/cassettes"))
    preset = load_preset(os.environ.get("REPLAY_PRESET", ""), os.environ.get("REPLAY_PRESET_JSON", ""))
    names = [r[0] for r in (preset or {}).get("routes", [])] + [(preset or {}).get("default_route", "default")]
    upstreams = configure(preset, os.environ.get("REPLAY_ROUTES") or "",
                          {n: os.environ.get(f"UPSTREAM_{n.upper()}", "") for n in names})
    replay = Replay(root, os.environ.get("REPLAY_MODE") or "replay", os.environ.get("REPLAY_MISS") or "404", upstreams)  # empty = default
    port = int(os.environ.get("PORT", "8080"))
    sys.stderr.write(f"cassette proxy on :{port}, mode={replay.mode}, miss={replay.miss}, routes={route_names()}, cassettes={root}\n")
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(replay)).serve_forever()


if __name__ == "__main__":
    main()

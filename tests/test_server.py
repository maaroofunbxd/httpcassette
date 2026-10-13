"""httpcassette.server: routes, keys, presets (no-record calls, host-keyed routes, stubs), the four modes against a
real server and a fake upstream, and the file run on its own."""
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from httpcassette import server as rp
from httpcassette import store

# A service with a search API, an auth API on another prefix, and model servers sharing one path that differ by
# Host; its login (a form at / or a POST with a password) must never be stored.
PRESET = {
    "name": "example",
    "routes": [["search", ["/v1/", "/v2/"]], ["auth", ["/oauth/"]]],
    "default_route": "models",
    "host_keyed": ["models"],
    "host_suffix": ".models.example.com",
    "no_record": [["models", "=/"], ["models", "body:password="], ["models", "/login"]],
    "no_record_cookie": "session=replay",
    "stubs": [{"route": "search", "path_contains": "/tag", "body": {"data": {"query": "{query.q}", "tags": []}}},
              {"route": "search", "body": {"data": {}, "errors": None}},
              {"route": "auth", "status": 401, "body": {"error": "stub"}}],
}


@pytest.fixture(autouse=True)
def reset():
    yield
    rp.configure()


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _call(base, path, method="GET", body=None, headers=None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_preset_routes_keys_login_stubs():
    rp.configure(PRESET)
    assert rp.route_of("/v1/tag") == "search" and rp.route_of("/oauth/token") == "auth" and rp.route_of("/predict") == "models"
    a = rp.request_key("GET", "/v1/items?q=30+in&z=1", b"")
    assert a == rp.request_key("get", "/v1/items?z=1&q=30+in", b"")  # query order does not matter
    assert a != rp.request_key("GET", "/v1/items?q=31+in&z=1", b"")
    assert rp.request_key("POST", "/v1/tag", b'{"q":"a"}') != rp.request_key("POST", "/v1/tag", b'{"q":"b"}')
    p1 = rp.request_key("POST", "/predict", b"{}", "a.models.example.com")
    assert p1 != rp.request_key("POST", "/predict", b"{}", "b.models.example.com")  # host-keyed route
    assert rp.request_key("POST", "/predict", b"{}", "x.elsewhere.org") == rp.request_key("POST", "/predict", b"{}", "y.elsewhere.org")
    assert rp.request_key("POST", "/v1/tag", b"{}", "a") == rp.request_key("POST", "/v1/tag", b"{}", "b")  # not host-keyed
    assert rp.is_login("GET", "/", b"") and rp.is_login("POST", "/session", b"user=x&password=y") and rp.is_login("POST", "/login/form", b"")
    assert not rp.is_login("GET", "/v1/tag", b"") and not rp.is_login("POST", "/predict", b"{}")
    assert rp.stub_answer("/v1/tag", "q=red") == (200, {"data": {"query": "red", "tags": []}})
    assert rp.stub_answer("/v1/items", "") == (200, {"data": {}, "errors": None})
    assert rp.stub_answer("/oauth/token", "") == (401, {"error": "stub"})
    assert rp.stub_answer("/predict", "") == (200, {})
    with pytest.raises(ValueError):
        rp.Replay(None, "bogus")
    with pytest.raises(ValueError):
        rp.Replay(None, "replay", miss="bogus")
    with pytest.raises(ValueError):
        rp.preset_config({"rutes": []})


def test_generic_routes_and_record(tmp_path):
    class Up(BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps({"echo": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    up, up_url = _serve(Up)
    try:
        ups = rp.configure(None, f"api=/v2/={up_url};rest=/={up_url}")
        assert rp.route_of("/v2/search") == "api" and rp.route_of("/other") == "rest"
        assert not rp.is_login("GET", "/", b"password=x")  # no preset: nothing is a login
        assert rp.stub_answer("/v2/x", "") == (200, {})
        assert rp.route_names() == ["api", "rest"]
        srv, base = _serve(rp.make_handler(rp.Replay(tmp_path, "record", upstreams=ups)))
        assert json.loads(_call(base, "/v2/search?q=red")[1])["echo"] == "/v2/search?q=red"
        srv.shutdown()
        assert [e.rel.split("/")[0] for e in store.entries(tmp_path)] == ["api"]
        for bad in ("api=/v2/", "=/x=http://u"):
            with pytest.raises(ValueError):
                rp.parse_routes(bad)
        ups = rp.configure(PRESET, f"api=/v2/={up_url}")  # REPLAY_ROUTES replaces the preset's routes, keeps the rest
        assert rp.route_of("/v1/tag") == "models" and rp.CONFIG.no_record_cookie == "session=replay" and ups == {"api": up_url}
    finally:
        up.shutdown()


def test_record_then_replay_then_miss_and_stub(tmp_path):
    rp.configure(PRESET)
    calls = []

    class Upstream(BaseHTTPRequestHandler):
        def _h(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            calls.append((self.command, self.path, body, self.headers.get("Host")))
            out = json.dumps({"data": {"items": [2.0]}, "echo": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        do_GET = do_POST = _h

        def log_message(self, *a):
            pass

    up, up_url = _serve(Upstream)
    rec = rp.Replay(tmp_path, "record", upstreams={"search": up_url, "auth": up_url, "models": up_url})
    srv, base = _serve(rp.make_handler(rec))
    s, body, _ = _call(base, "/v1/items?q=2+kg")
    assert s == 200 and json.loads(body)["data"]["items"] == [2.0]
    s, _, _ = _call(base, "/", "POST", b"user=ai&password=secret", {"Content-Type": "application/x-www-form-urlencoded"})
    assert s == 200  # login forwarded ...
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1 and files[0].parent.name == "search"  # ... but never stored
    assert "secret" not in "".join(f.read_text() for f in files)
    _call(base, "/predict", "POST", b"{}", {"Host": "a.models.example.com", "Content-Type": "application/json"})
    assert calls[-1][3] == "a.models.example.com"  # Host forwarded for host-routed upstreams
    assert json.loads(next((tmp_path / "models").glob("*.json")).read_text())["request"]["host"] == "a.models.example.com"
    srv.shutdown()
    up.shutdown()

    srv, base = _serve(rp.make_handler(rp.Replay(tmp_path, "replay")))
    s, body, _ = _call(base, "/v1/items?q=2+kg")
    assert s == 200 and json.loads(body)["data"]["items"] == [2.0]
    s, body, _ = _call(base, "/v1/items?q=3+kg")
    assert s == 404 and "q=3+kg" in json.loads(body)["key"]
    s, _, h = _call(base, "/")
    assert s == 200 and "session=replay" in h.get("Set-Cookie", "")
    s, body, _ = _call(base, "/__replay/stats")
    assert json.loads(body)["hit"] == 1 and json.loads(body)["miss"] == 1 and json.loads(body)["login"] == 1
    srv.shutdown()

    srv, base = _serve(rp.make_handler(rp.Replay(tmp_path, "replay", miss="stub")))
    s, body, _ = _call(base, "/v1/items?q=3+kg")
    assert s == 200 and json.loads(body) == {"data": {}, "errors": None}
    srv.shutdown()
    srv, base = _serve(rp.make_handler(rp.Replay(tmp_path / "none", "stub")))
    s, body, _ = _call(base, "/v1/tag?q=blue+tee")
    assert s == 200 and json.loads(body)["data"] == {"query": "blue tee", "tags": []}
    srv.shutdown()


def test_auto_mode_reports_upstream_errors(tmp_path):
    rp.configure(PRESET)
    srv, base = _serve(rp.make_handler(rp.Replay(tmp_path, "auto", upstreams={"search": "http://127.0.0.1:9"})))
    s, body, _ = _call(base, "/v1/tag?q=x")
    assert s == 502 and "upstream" in json.loads(body)["error"]
    assert not list(tmp_path.rglob("*.json"))
    s, body, _ = _call(base, "/oauth/token")  # a route with no upstream
    assert s == 502 and "UPSTREAM_AUTH" in json.loads(body)["error"]
    srv.shutdown()


def _run_standalone(server_file, env, path):
    """Run the proxy file as a script (plain python, no package on the path) and GET one path from it. It binds
    0.0.0.0, whose getfqdn() can take seconds on a slow resolver: hence the patient wait."""
    import socket
    import subprocess
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-I", str(server_file)], env={**env, "PORT": str(port), "REPLAY_QUIET": "1"},
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(server_file.parent))
    try:
        for _ in range(100):
            try:
                return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2).read())
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError(proc.stderr.read().decode())
                time.sleep(0.05)
        raise AssertionError("server did not start")
    finally:
        proc.kill()
        proc.wait()


def test_file_runs_alone_with_a_preset_file_or_inline(tmp_path):
    """Copied into an image next to NAME.json (REPLAY_PRESET=NAME), or alone onto a volume with the preset in
    REPLAY_PRESET_JSON: both answer with the preset's stubs."""
    import shutil

    want = {"query": "red", "tags": []}
    image = tmp_path / "app"
    image.mkdir()
    shutil.copy(rp.__file__, image / "server.py")
    (image / "example.json").write_text(json.dumps(PRESET))
    env = {"REPLAY_MODE": "stub", "REPLAY_DIR": str(tmp_path / "c")}
    assert _run_standalone(image / "server.py", {**env, "REPLAY_PRESET": "example"}, "/v1/tag?q=red")["data"] == want

    alone = tmp_path / "vol"
    alone.mkdir()
    shutil.copy(rp.__file__, alone / "server.py")
    out = _run_standalone(alone / "server.py", {**env, "REPLAY_PRESET": "example", "REPLAY_PRESET_JSON": json.dumps(PRESET)}, "/v1/tag?q=red")
    assert out["data"] == want
    assert rp.load_preset("") is None and rp.load_preset("none") is None
    with pytest.raises(ValueError):
        rp.load_preset("example")  # no example.json next to the package's copy


def test_modules_are_standalone():
    """server.py runs as a copied file: stdlib + requests, nothing from httpcassette. store.py is stdlib only."""
    stdlib = getattr(sys, "stdlib_module_names", None)  # 3.10+
    for mod, allowed in ((rp, {"requests"}), (store, set())):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+(httpcassette|\.)", text, re.M), mod.__name__
        top = {m.split(".")[0] for m in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M)} - {"__future__"}
        if stdlib:
            assert top - set(stdlib) <= allowed, (mod.__name__, top - set(stdlib))

"""httpcassette.store: reading, summarising and comparing cassette sets (no service involved)."""
import json

from httpcassette import store


def write(root, route, name, path, query, status, body, req_body=""):
    d = root / route
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"request": {"method": "GET", "path": path, "query": query, "host": "", "body": req_body},
                                                "status": status, "content_type": "application/json", "body": body}))


def test_entries_summary_diff(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, "api", "k1", "/v1/accounts/acct-1/items", "q=30+inch", 200, '{"data": {"x": 1, "y": 2}}')
    write(a, "api", "k2", "/v1/accounts/acct-1/items", "q=2+kg", 200, '{"data": {"x": 1}}')
    write(a, "api", "k3", "/v1/search", "q=chair", 200, '{"tags": []}')
    write(b, "api", "k1", "/v1/accounts/acct-1/items", "q=30+inch", 200, '{"data": {"y": 2, "x": 1}}')  # same JSON, other key order
    write(b, "api", "k2", "/v1/accounts/acct-1/items", "q=2+kg", 200, '{"data": {"x": 9}}')
    write(b, "auth", "k9", "/token", "", 404, "{}")
    (a / "api" / "broken.json").write_text("not json")
    assert [e.rel for e in store.entries(a)] == ["api/k1.json", "api/k2.json", "api/k3.json"]  # unreadable skipped
    s = store.summary(a, ("acct-",))
    assert s["api"]["count"] == 3 and s["api"]["status"] == {200: 3}
    assert ("GET /v1/accounts/*/items", 2) in s["api"]["paths"]
    d = store.diff(a, b)
    assert [x.rel for x, _ in d["same"]] == ["api/k1.json"] and [x.rel for x, _ in d["differs"]] == ["api/k2.json"]
    assert [x.rel for x in d["only_a"]] == ["api/k3.json"] and [y.rel for y in d["only_b"]] == ["auth/k9.json"]
    assert store.body_diff('{"data": {"x": 1}}', '{"data": {"x": 9}}') == ["/data/x: 1 vs 9"]
    assert store.body_diff("a", "b") == ["(not JSON: bodies differ as text)"]
    assert store.human_bytes(2048) == "2 KB"


def test_path_shape(tmp_path):
    assert store.path_shape("/v1/users/u12345678/orders") == "/v1/users/*/orders"  # digits, longer than 6
    assert store.path_shape("/v1/users/u123/orders") == "/v1/users/u123/orders"
    assert store.path_shape("/v1/" + "x" * 41) == "/v1/*"
    assert store.path_shape("/v1/accounts/acct-a/items", ("acct-",)) == "/v1/accounts/*/items"
    write(tmp_path, "api", "k1", "/v1/accounts/acct-a/items", "", 200, "{}")
    assert store.summary(tmp_path)["api"]["paths"] == [("GET /v1/accounts/acct-a/items", 1)]
    assert store.summary(tmp_path, ("acct-",))["api"]["paths"] == [("GET /v1/accounts/*/items", 1)]


def test_missing_dir(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        list(store.entries(tmp_path / "nope"))

# httpcassette

Record, replay and stub any HTTP service from cassettes: one JSON file per request, so a set of them diffs and
merges like source. For running a program with its HTTP dependencies replaced by recorded answers (offline, in CI,
in a load test), and for comparing two environments' answers to the same calls.

- `httpcassette.server`: a proxy with four modes. **replay** answers from cassettes (a miss is a 404 carrying the
  request key), **record** forwards to the real service and stores each answer, **auto** replays and records the
  misses, **stub** answers every call with its route's empty-but-valid body, no recordings needed.
- `httpcassette.store`: list, summarise and compare cassette sets.

## Install

```bash
uv add "httpcassette @ git+https://github.com/maaroofunbxd/httpcassette@v0.1.0"
```

Python 3.9 or newer. Depends on requests, used only to forward (record and auto); `httpcassette[socks]` for
upstreams behind a SOCKS proxy (`ALL_PROXY=socks5h://…`).

## Cassettes

`<dir>/<route>/<sha256 of the request key>.json`, one file per request:

```json
{"request": {"method": "GET", "path": "/v2/search", "query": "q=red", "host": "", "body": ""},
 "status": 200, "content_type": "application/json", "body": "{\"hits\": []}"}
```

The key is method + path + sorted query + (the Host header, for host-keyed routes) + request body: two calls with
the same key are the same call. Answers with status 500 or more are never recorded.

## The proxy

Run it installed (`python -m httpcassette.server`) or as a copied file (`python server.py`): `server.py` is one
file with the standard library and requests (imported only to forward), so it can be copied into an image or onto
a volume next to the program it serves. Settings come from the environment:

| Variable | Meaning (default) |
|---|---|
| `REPLAY_DIR` | the cassette dir (`/cassettes`) |
| `REPLAY_MODE` | `replay` \| `record` \| `auto` \| `stub` (`replay`) |
| `REPLAY_MISS` | `404` \| `stub`: what a replay miss gets (`404`) |
| `PORT` | listen port (`8080`) |
| `REPLAY_ROUTES` | `name=/pfx1,/pfx2=http://upstream;other=/=http://u2`: routes by path prefix, first match wins; prefix `/` is the default route |
| `UPSTREAM_<ROUTE>` | a route's upstream, for routes that come from a preset |
| `REPLAY_PRESET` | `none` \| `NAME` (`NAME.json` next to server.py) \| a `.json` path |
| `REPLAY_PRESET_JSON` | the preset itself (wins over `REPLAY_PRESET`), for when server.py travels alone |
| `REPLAY_QUIET=1` | no line per request on stderr |

`GET /__replay/stats` returns the mode and the hit / miss / recorded / stub counts.

```bash
# capture a service's answers, then serve them offline
REPLAY_DIR=./cass REPLAY_MODE=record REPLAY_ROUTES='api=/=https://api.example.com' python -m httpcassette.server
REPLAY_DIR=./cass REPLAY_MODE=replay REPLAY_ROUTES='api=/=http://unused' PORT=9000 python -m httpcassette.server
```

### Presets

What the proxy stands in for, as a JSON object (all keys optional):

| Key | Meaning |
|---|---|
| `routes` | `[[name, [path prefix, ...]], ...]`, first match wins |
| `default_route` | the route of a path matching no prefix (`default`) |
| `host_keyed`, `host_suffix` | routes whose Host header (ending `host_suffix`; `""` = any) is part of the key and forwarded upstream: services that share a path and differ by Host |
| `no_record`, `no_record_cookie` | `[[route, rule], ...]` calls never written to a cassette (a login): rule = a path prefix or substring, `=/` (GET of `/` only) or `body:<substring>`. Record forwards them without storing; replay answers 200 with `no_record_cookie` as Set-Cookie |
| `stubs` | `[{"route", "path_contains"?, "status"?, "body"}, ...]`, first match wins, else 200 `{}`; a string `"{query.NAME}"` in a body becomes that query parameter's value |

`REPLAY_ROUTES`, when set, replaces the preset's routes and keeps the rest.

In Python: `upstreams = server.configure(preset, routes)`, then
`ThreadingHTTPServer((host, port), server.make_handler(server.Replay(dir, mode, miss, upstreams)))`.

## The store

```python
from pathlib import Path
from httpcassette import store

store.summary(Path("cass"), id_prefixes=("user-",))  # per route: count, bytes, statuses, dates, top path shapes
for x, y in store.diff(Path("prod"), Path("staging")).get("differs", []):
    print(x.rel, x.status, y.status, store.body_diff(x.response, y.response))
```

`path_shape` blanks variable path segments for grouping (has digits and is longer than 6, longer than 40, or
starts with one of `id_prefixes`).

## Development

```bash
uv sync && uv run ruff check src tests && uv run pytest -q
```

## License

MIT, see [LICENSE](LICENSE).

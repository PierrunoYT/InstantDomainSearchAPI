# InstantDomainSearch API

A REST/JSON API for domain availability that speaks the same web protocol as
instantdomainsearch.com (`/services/*`). **Unofficial**: the endpoints can change,
rate-limit, or block non-browser clients, and using them may be against the site's
terms. Check before relying on it.

## Run

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; use .venv/bin/pip elsewhere
.venv/Scripts/python main.py                     # http://127.0.0.1:8000/docs
```

Env vars: `HOST`, `PORT`.

## Endpoints

| Method | Path | Example |
| --- | --- | --- |
| GET | `/search/{name}` | `/search/acme?tlds=com,io,ai` |
| GET | `/search/{name}/stream` | NDJSON, one line per result |
| GET | `/check` | `/check?domains=acme.com,acme.io` |
| POST | `/check` | `{"domains": ["acme.com", "acme.io"]}` |
| GET | `/extensions/{name}` | `/extensions/acme?tld=com` |
| GET | `/spell/{name}` | |
| GET | `/health` | |

Errors: `422` for malformed names, `502` with the upstream reason on failure.

## Protocol notes

- One plain `GET` per query, no WebSocket. Search asks `/services/check` for the `.com`
  plus the ~70 TLD batch, in batches of 100 names.
- Every call needs `hash`: Java-style string hash seeded with 42 (`h = h*31 + codepoint`,
  int32). `/check` hashes the full comma-joined `names` string. A wrong hash returns
  `invalid hash`. `/spell` needs none.
- `/check` answers 403 unless the request looks like a browser; the client sends a
  browser `User-Agent` and a `Referer` from the site.
- Records look like `{"name": "acme.com", "availability": "inuse", "trust": "medium"}`.
  Availability is one of `available`, `inuse`, `reserved`, `blocked`, `unsupported`.
- `/check` also returns `unsupported` for extensions it does not cover, so filter on it
  if you only want real answers.

## Known limits

- The site's own `zone-names` / `dns-names` endpoints still answer `200`, but return
  `{"results": []}` for every query (checked October 2026 against the exact URLs the
  site requests in a browser). They are kept only as background; search uses `/check`.
- `trust` is upstream's confidence, not a guarantee.
- Treat results as hints and confirm with a registrar.

## Tests

```sh
.venv/Scripts/python -m pytest
```

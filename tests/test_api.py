import json

import httpx
import pytest
from fastapi.testclient import TestClient

from instantdomainsearch.api import app, get_client
from instantdomainsearch.instant import InstantClient, normalize_domain, normalize_label, site_hash


def make_client(requests, handler=None, status=200):
    def default(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="403 Forbidden")
        path = request.url.path
        if path.startswith("/services/spell/"):
            return httpx.Response(200, json={"label": "acme", "suggestion": None})
        if path.endswith("/check"):
            names = [n for n in request.url.params.get("names", "").split(",") if n]
            return httpx.Response(
                200,
                json={"results": [{"name": n, "availability": "available"} for n in names]},
            )
        return httpx.Response(200, json={"results": [{"domain": "acme.com"}]})

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (handler or default)(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return InstantClient(base_url="https://example.test", http=http)


@pytest.fixture
def requests():
    reqs: list[httpx.Request] = []
    app.dependency_overrides[get_client] = lambda: make_client(reqs)
    yield reqs
    app.dependency_overrides.clear()


http = TestClient(app)


def test_site_hash_matches_observed_browser_request():
    assert site_hash("fastdomaincheckerxyz") == "-983138034"
    assert site_hash("") == "42"


def test_normalize():
    assert normalize_label("Blue Harbor") == "blueharbor"
    assert normalize_domain(" Shop.CO.UK ") == "shop.co.uk"
    for bad in ("acme.com", "-acme", "a_b"):
        with pytest.raises(ValueError):
            normalize_label(bad)
    with pytest.raises(ValueError):
        normalize_domain("acme")


def test_search_com_first_then_wide_with_valid_hash(requests):
    r = http.get("/search/Acme", params={"tlds": "com,io,ai"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "acme"
    assert [i["phase"] for i in body["results"]] == ["com", "wide", "wide"]
    assert {i["source"] for i in body["results"]} == {"check"}
    assert [i["record"]["name"] for i in body["results"]] == ["acme.com", "acme.io", "acme.ai"]
    assert len(requests) == 1
    q = requests[0].url.params
    assert q["names"] == "acme.com,acme.io,acme.ai"
    assert q["hash"] == site_hash("acme.com,acme.io,acme.ai")
    assert requests[0].headers["accept"] == "application/json"


def test_search_batches_long_tld_lists(requests):
    tlds = [f"t{i}" for i in range(250)]
    r = http.get("/search/acme", params={"tlds": ",".join(tlds), "limit": 1000})
    assert r.status_code == 200
    assert len(requests) == 3  # 251 names -> 100 + 100 + 51
    assert all(len(q.url.params["names"].split(",")) <= 100 for q in requests)


def test_search_stream_is_ndjson(requests):
    r = http.get("/search/acme/stream", params={"tlds": "com"})
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert lines and all(item["phase"] == "com" for item in lines)


def test_check_hashes_the_names_string(requests):
    r = http.post("/check", json={"domains": ["Acme.com", "acme.io", "ACME.com"]})
    assert r.status_code == 200
    q = requests[0].url.params
    assert q["names"] == "acme.com,acme.io"
    assert q["hash"] == site_hash("acme.com,acme.io")


def test_spell_and_extensions(requests):
    assert http.get("/spell/acme").json()["label"] == "acme"
    assert http.get("/extensions/acme").status_code == 200
    ext = requests[-1].url.params
    assert ext["filter"] == "remove_registered,remove_aftermarkets"


def test_invalid_input_is_422(requests):
    assert http.get("/search/acme.com").status_code == 422
    assert http.get("/search/acme.com/stream").status_code == 422
    assert http.get("/check", params={"domains": "nodot"}).status_code == 422
    assert requests == []


def test_upstream_403_is_502():
    app.dependency_overrides[get_client] = lambda: make_client([], status=403)
    try:
        r = http.get("/check", params={"domains": "acme.com"})
        assert r.status_code == 502
        assert "403" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()

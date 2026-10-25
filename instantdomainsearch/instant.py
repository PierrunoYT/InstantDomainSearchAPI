"""Client for the web endpoints behind instantdomainsearch.com (``/services/*``).

Protocol, as observed from the site's own browser traffic and JS bundle:

* Plain ``GET`` requests, one per query, no WebSocket.
* Every call carries ``hash``: a Java-style string hash of the queried text seeded
  with 42, rendered as a signed 32-bit decimal. The server rejects a wrong hash
  with ``invalid hash``.
* Availability comes from ``check``: ``names`` is one comma-joined domain list and
  ``hash`` is the hash of that whole string. It answers
  ``{"results": [{"name", "availability", "trust"}]}``.
* The site's older ``zone-names``/``dns-names`` endpoints still answer 200, but now
  return ``{"results": []}`` for every query (observed October 2026, including the
  exact URLs the site itself requests), so search is built on ``check`` instead.
* ``check`` answers 403 unless the request looks like a browser: a normal browser
  ``User-Agent`` (or a ``Referer`` from the site) is enough.

These endpoints are unofficial: they may change, rate-limit, or answer 403/empty.
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

import httpx

BASE_URL = "https://instantdomainsearch.com"
HASH_SEED = 42

# TLDs the site requests in its second, wide batch (in its order).
DEFAULT_TLDS = (
    "com net org ai io xyz app online info co store site bond dev tech pro live lol news "
    "group business network chat technology events blog link photos travel me cloud design "
    "studio art fun world digital global space plus media email host page ltd biz agency "
    "social zone website team life best cool today guru care marketing luxury solutions "
    "services money consulting bio inc fm new video legal day llc coffee"
).split()

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*$")


class InstantError(RuntimeError):
    """The upstream service failed or rejected the call."""


def site_hash(text: str, seed: int = HASH_SEED) -> str:
    """The ``hash`` query parameter: ``h = h*31 + codepoint`` in int32, seeded with 42."""
    h = seed
    for ch in text:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return str(h - (1 << 32) if h >= 1 << 31 else h)


def normalize_label(name: str) -> str:
    """'Blue Harbor' -> 'blueharbor'. Raises ValueError if not a valid label."""
    label = re.sub(r"\s+", "", name).lower()
    if not _LABEL_RE.match(label):
        raise ValueError(f"invalid name {name!r}: use letters, digits and hyphens, no extension")
    return label


def normalize_tld(tld: str) -> str:
    tld = tld.strip().lower().lstrip(".")
    if not _TLD_RE.match(tld):
        raise ValueError(f"invalid extension {tld!r}")
    return tld


def normalize_domain(domain: str) -> str:
    """'Acme.COM' -> 'acme.com'. The first dot separates label from extension."""
    domain = domain.strip().lower().rstrip(".")
    label, sep, tld = domain.partition(".")
    if not sep:
        raise ValueError(f"invalid domain {domain!r}: missing extension")
    return f"{normalize_label(label)}.{normalize_tld(tld)}"


def _records(text: str) -> list[Any]:
    """Parse a body that is either one JSON document or NDJSON; flatten ``{"results": [...]}``."""
    text = text.strip()
    if not text:
        return []
    try:
        docs = [json.loads(text)]
    except json.JSONDecodeError:
        try:
            docs = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise InstantError(f"unparseable response: {text[:120]!r}") from exc
    out: list[Any] = []
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("results"), list):
            out.extend(doc["results"])
        else:
            out.append(doc)
    return out


_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://instantdomainsearch.com/",
    "Origin": "https://instantdomainsearch.com",
}

# ``/services/check`` accepts a long comma-joined ``names`` list; keep each URL modest.
CHECK_BATCH = 100


class InstantClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/services/{path}"

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise InstantError(f"HTTP {resp.status_code}: {resp.text[:120].strip() or resp.reason_phrase}")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        try:
            resp = await self._http.get(self._url(path), params=params, headers=_HEADERS)
        except httpx.HTTPError as exc:
            raise InstantError(f"{path}: {exc}") from exc
        self._raise_for(resp)
        return _records(resp.text)

    def _name_params(self, label: str, tlds: list[str], limit: int, extra: dict[str, Any] | None = None):
        params = {"hash": site_hash(label), "limit": limit, "tlds": ",".join(tlds)}
        params.update(extra or {})
        return params

    async def stream_search(
        self, name: str, tlds: list[str] | None = None, limit: int = 144
    ) -> AsyncIterator[dict[str, Any]]:
        """Availability for ``name`` across the extensions, ``.com`` first.

        Uses ``/services/check``: the older ``zone-names``/``dns-names`` endpoints still
        answer 200 but now return ``{"results": []}`` for every query.

        Yields ``{"phase", "source", "record"}`` with one upstream availability record.
        """
        label = normalize_label(name)
        wide = [normalize_tld(t) for t in tlds] if tlds else list(DEFAULT_TLDS)
        domains = list(dict.fromkeys(f"{label}.{t}" for t in ["com", *wide]))
        records = [r for r in await self._check_batch(domains) if isinstance(r, dict)]
        records.sort(key=lambda r: 0 if str(r.get("name", "")).endswith(".com") else 1)
        for rec in records[:limit]:
            phase = "com" if str(rec.get("name", "")).endswith(".com") else "wide"
            yield {"phase": phase, "source": "check", "record": rec}

    async def _check_batch(self, names: list[str]) -> list[Any]:
        out: list[Any] = []
        for i in range(0, len(names), CHECK_BATCH):
            joined = ",".join(names[i : i + CHECK_BATCH])
            out.extend(await self._get("check", {"names": joined, "hash": site_hash(joined)}))
        return out

    async def search(self, name: str, tlds: list[str] | None = None, limit: int = 144) -> dict[str, Any]:
        label = normalize_label(name)
        results = [item async for item in self.stream_search(label, tlds, limit)]
        return {"name": label, "results": results}

    async def check(self, domains: list[str]) -> list[Any]:
        """Specific full domain names via ``/services/check``."""
        names = list(dict.fromkeys(normalize_domain(d) for d in domains))
        return await self._check_batch(names)

    async def extensions(self, name: str, tld: str = "com", limit: int = 72) -> list[Any]:
        """Alternative extensions for a name, registered and aftermarket names removed."""
        label = normalize_label(name)
        params = self._name_params(
            label, [normalize_tld(tld)], limit, {"filter": "remove_registered,remove_aftermarkets"}
        )
        return await self._get(f"extension-fix/{label}", params)

    async def spell(self, name: str) -> Any:
        """Spelling suggestion. Needs no hash."""
        records = await self._get(f"spell/{normalize_label(name)}")
        return records[0] if records else {}

"""FastAPI app exposing domain search as a plain REST/JSON API."""

from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .instant import InstantClient, InstantError, normalize_label

app = FastAPI(
    title="InstantDomainSearch API",
    description="REST API over the web endpoints behind instantdomainsearch.com. "
    "Unofficial: upstream may change, rate-limit or block requests.",
    version="0.2.0",
)

_client = InstantClient()


def get_client() -> InstantClient:
    return _client


class CheckRequest(BaseModel):
    domains: list[str] = Field(..., min_length=1, max_length=500, examples=[["acme.com", "acme.io"]])


def _split(csv: str | None) -> list[str]:
    return [p for p in (s.strip() for s in (csv or "").split(",")) if p]


async def _run(coro):
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InstantError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/search/{name}")
async def search(
    name: str,
    tlds: str | None = Query(None, description="Comma-separated extensions; default is the site's ~70"),
    limit: int = Query(144, ge=1, le=1000),
    client: InstantClient = Depends(get_client),
):
    """Collected results: `.com` first, then the wide batch, queried via `/services/check`."""
    return await _run(client.search(name, _split(tlds) or None, limit))


@app.get("/search/{name}/stream")
async def search_stream(
    name: str,
    tlds: str | None = Query(None),
    limit: int = Query(144, ge=1, le=1000),
    client: InstantClient = Depends(get_client),
):
    """Same as `/search/{name}`, but NDJSON, one line per result."""
    try:
        normalize_label(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def lines() -> AsyncIterator[str]:
        try:
            async for item in client.stream_search(name, _split(tlds) or None, limit):
                yield json.dumps(item) + "\n"
        except (InstantError, ValueError) as exc:
            yield json.dumps({"error": str(exc)}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/check")
async def check_get(
    domains: str = Query(..., description="Comma-separated full domains, e.g. acme.com,acme.io"),
    client: InstantClient = Depends(get_client),
):
    names = _split(domains)
    if not names:
        raise HTTPException(status_code=422, detail="no domains given")
    return {"results": await _run(client.check(names))}


@app.post("/check")
async def check_post(body: CheckRequest, client: InstantClient = Depends(get_client)):
    return {"results": await _run(client.check(body.domains))}


@app.get("/extensions/{name}")
async def extensions(
    name: str,
    tld: str = Query("com"),
    limit: int = Query(72, ge=1, le=1000),
    client: InstantClient = Depends(get_client),
):
    """Alternative extensions for a name (registered and aftermarket names removed)."""
    return {"results": await _run(client.extensions(name, tld, limit))}


@app.get("/spell/{name}")
async def spell(name: str, client: InstantClient = Depends(get_client)):
    return await _run(client.spell(name))

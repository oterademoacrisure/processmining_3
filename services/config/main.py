"""config-service — independent API for operator-managed source configuration.

Each entry is (key, value):  # SOURCE-CONFIG: source_type removed
    key   — the component / instance name, e.g. vote
    value — how to reach it, e.g. http://host/logs?app=vote&tail=200

This service owns validation and the public contract; it has NO database
connection. Saves, lists and deletes are
forwarded to the aggregator (core-service), which persists them in the
source_config table.

Endpoints:
    GET    /health
    POST   /configs                   — validate + save (upsert) one entry
    GET    /configs?tenant_id=        — list saved entries
    DELETE /configs?tenant_id=&key=
                                      — remove one entry

Run locally:
    uvicorn services.config.main:app --host 0.0.0.0 --port 8200
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator


log = logging.getLogger("config-service")

# SOURCE-CONFIG: keep key a plain identifier.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _aggregator() -> httpx.Client:
    base = os.getenv("AGGREGATOR_SERVICE_URL", "http://localhost:8000").rstrip("/")
    return httpx.Client(base_url=base, timeout=float(os.getenv("AGGREGATOR_HTTP_TIMEOUT", "10")))


app = FastAPI(title="serverops-config", version="1.1.0")


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "config"}


class ConfigIn(BaseModel):
    tenant_id: int = Field(1, ge=1)
    key: str = Field(..., max_length=128)
    value: str = Field(..., max_length=2048)

    @field_validator("key", "value")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if not _KEY_RE.match(v):
            raise ValueError("use letters, digits, '-', '_' or '.' (e.g. vote, redis-01)")
        return v

    # SOURCE-CONFIG: value may be a URL or free text (e.g. a path); if it is a
    # URL, never allow credentials embedded in it.
    @field_validator("value")
    @classmethod
    def _no_url_credentials(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme in ("http", "https") and (parts.username or parts.password):
            raise ValueError("do not put credentials in the URL — store them in Key Vault")
        return v


def _forward(method: str, path: str, **kwargs: Any) -> Any:
    try:
        with _aggregator() as c:
            r = c.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        log.warning("aggregator unreachable: %s", e)
        raise HTTPException(status_code=502, detail=f"aggregator unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="not found")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"aggregator error {r.status_code}: {r.text}")
    return r.json()


@app.post("/configs")
def save_config(body: ConfigIn) -> dict[str, Any]:
    return _forward("PUT", "/api/v1/source-configs", json=body.model_dump())


@app.get("/configs")
def list_configs(tenant_id: int = Query(1, ge=1)) -> list[dict[str, Any]]:
    return _forward("GET", "/api/v1/source-configs", params={"tenant_id": tenant_id})


@app.delete("/configs")
def delete_config(
    key: str,
    tenant_id: int = Query(1, ge=1),
) -> dict[str, Any]:
    return _forward(
        "DELETE", "/api/v1/source-configs",
        params={"tenant_id": tenant_id, "key": key},
    )


if __name__ == "__main__":
    # `python -m services.config.main` — dev only.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8200")))

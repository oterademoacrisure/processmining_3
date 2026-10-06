"""APIFICATION: core-service HTTP routes (UI-facing).

Four groups:
  1. /query          — named-view escape hatch for the UI's read paths
  2. /actions/...    — write path for operator decisions (wraps decide_action)
  3. /precedent/...  — read paths for the FAISS-backed Precedent Memory page
  4. /source-configs — SOURCE-CONFIG: key/value storage (Source Config page)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text  # SOURCE-CONFIG: delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert  # SOURCE-CONFIG: upsert

from app.db.models import ConfigKV, HistoricalIncident  # SOURCE-CONFIG: ConfigKV
from app.db.session import SessionLocal
from app.paths import PROJECT_ROOT
from app.remediation.decisions import decide_action

from services.core.api.query_views import resolve


log = logging.getLogger("core.api")

router = APIRouter(prefix="/api/v1")


# ── /query — the named-view escape hatch ────────────────────────────────────
class QueryRequest(BaseModel):
    view: str
    params: dict[str, Any] = {}


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


def _jsonify(value: Any) -> Any:
    """Best-effort JSON-safe conversion for DB values (datetimes, jsonb dicts,
    Decimals, UUIDs). Falls back to str() for anything else exotic."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)


@router.post("/query", response_model=QueryResponse)
def run_query(body: QueryRequest) -> QueryResponse:
    try:
        sql, params = resolve(body.view, body.params)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        with SessionLocal() as s:
            result = s.execute(text(sql), params)
            rows = result.fetchall()
            cols = list(result.keys())
    except Exception as e:
        log.exception("query %s failed", body.view)
        raise HTTPException(status_code=500, detail=f"query failed: {e}")

    return QueryResponse(
        columns=cols,
        rows=[[_jsonify(v) for v in r] for r in rows],
    )


# ── /actions/{id}/decide — write path ──────────────────────────────────────
class DecideRequest(BaseModel):
    new_state: str
    approver_id: str = "anonymous"
    note: str = ""


class DecideResponse(BaseModel):
    ok: bool
    snow_posted: bool
    message: str


@router.post("/actions/{action_id}/decide", response_model=DecideResponse)
def action_decide(action_id: int, body: DecideRequest) -> DecideResponse:
    result = decide_action(
        action_id=action_id,
        new_state=body.new_state,
        approver_id=body.approver_id,
        note=body.note,
    )
    # POINT 21 telemetry moved server-side (was a fail-safe try/except in the UI).
    if result.get("ok") and body.new_state == "approved":
        try:
            with SessionLocal() as s:
                row = s.execute(text(
                    "SELECT r.payload->>'timeline_correlation_id' AS corr, ra.tenant_id AS tenant "
                    "FROM remediation_action ra "
                    "JOIN root_cause_report r ON r.report_id = ra.report_id "
                    "WHERE ra.action_id = :a"
                ), {"a": action_id}).first()
            if row and row.corr:
                from app.observability import progress
                progress.emit(
                    "APPROVAL", "succeeded",
                    tenant_id=int(row.tenant),
                    correlation_id=row.corr,
                    step="human approved",
                    message=f"approved by {body.approver_id}",
                )
        except Exception:
            log.debug("APPROVAL telemetry emit failed (non-fatal)", exc_info=True)

    return DecideResponse(
        ok=bool(result.get("ok")),
        snow_posted=bool(result.get("snow_posted")),
        message=str(result.get("message") or ""),
    )


# ── /precedent — the FAISS-backed Precedent Memory page ────────────────────
class PrecedentStatus(BaseModel):
    backend: str
    vectors_indexed: int
    counts: dict[str, int]  # approved / rejected / unknown
    incidents: list[dict[str, Any]]  # for the "All indexed incidents" table


class PrecedentSearchRequest(BaseModel):
    tenant_id: int
    query: str
    k: int = 5


class PrecedentHit(BaseModel):
    id: int
    score: float
    number: str | None = None
    cmdb_ci: str | None = None
    outcome: str | None = None
    short_description: str | None = None
    close_notes: str | None = None


class PrecedentSearchResponse(BaseModel):
    embedder_mode: str
    hits: list[PrecedentHit]


def _precedent_cfg() -> dict[str, Any]:
    return {
        "vector_backend": "faiss",
        "vector_index_path": str(PROJECT_ROOT / ".data" / "precedent.faiss"),
        "embed_dim": 1536,
    }


@router.get("/precedent/status", response_model=PrecedentStatus)
def precedent_status(tenant_id: int = 1) -> PrecedentStatus:
    from app.vectorstore import get_vector_store  # lazy import

    cfg = _precedent_cfg()
    try:
        store = get_vector_store(cfg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"could not load vector index: {e}")

    meta_path = Path(cfg["vector_index_path"] + ".meta.json")
    ids: list[int] = []
    if meta_path.exists():
        try:
            ids = [int(k) for k in json.loads(meta_path.read_text(encoding="utf-8")).keys()]
        except Exception:
            log.exception("could not read FAISS meta at %s", meta_path)

    rows: list[HistoricalIncident] = []
    if ids:
        with SessionLocal() as s:
            rows = list(s.scalars(
                select(HistoricalIncident)
                .where(HistoricalIncident.incident_id.in_(ids))
                .where(HistoricalIncident.tenant_id == tenant_id)
            ).all())

    counts = {"approved": 0, "rejected": 0, "unknown": 0}
    for r in rows:
        key = (r.outcome or "unknown")
        counts[key] = counts.get(key, 0) + 1

    incidents = [
        {
            "id": r.incident_id,
            "ticket": r.number,
            "outcome": r.outcome,
            "system": r.cmdb_ci,
            "problem": (r.short_description or "")[:80],
        }
        for r in sorted(rows, key=lambda x: x.incident_id)
    ]

    return PrecedentStatus(
        backend=cfg["vector_backend"],
        vectors_indexed=store.count(),
        counts=counts,
        incidents=incidents,
    )


@router.post("/precedent/search", response_model=PrecedentSearchResponse)
def precedent_search(body: PrecedentSearchRequest) -> PrecedentSearchResponse:
    from app.vectorstore import Embedder, get_vector_store  # lazy import

    cfg = _precedent_cfg()
    try:
        store = get_vector_store(cfg)
        emb = Embedder(cfg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"vectorstore init failed: {e}")

    try:
        hits = store.search(emb.embed_one(body.query), k=body.k, tenant_id=body.tenant_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"search failed: {e}")

    # Join hits to HistoricalIncident rows for display fields.
    ids = [h.id for h in hits]
    by_id: dict[int, HistoricalIncident] = {}
    if ids:
        with SessionLocal() as s:
            rows = list(s.scalars(
                select(HistoricalIncident)
                .where(HistoricalIncident.incident_id.in_(ids))
                .where(HistoricalIncident.tenant_id == body.tenant_id)
            ).all())
        by_id = {r.incident_id: r for r in rows}

    out = []
    for h in hits:
        r = by_id.get(h.id)
        out.append(PrecedentHit(
            id=h.id,
            score=float(h.score),
            number=(r.number if r else None),
            cmdb_ci=(r.cmdb_ci if r else None),
            outcome=(r.outcome if r else None),
            short_description=(r.short_description if r else None),
            close_notes=(r.close_notes if r else None),
        ))
    return PrecedentSearchResponse(embedder_mode=getattr(emb, "mode", "unknown"), hits=out)


# ── /source-configs — key/value storage for the Source Config page ──────────
# SOURCE-CONFIG: rows live in config_kv, one per (tenant_id, key). PUT creates
# a key or changes its value; the key itself never changes.
class SourceConfigIn(BaseModel):
    tenant_id: int = 1
    key: str
    value: str


def _source_config_dict(r: ConfigKV) -> dict[str, Any]:
    return {
        "tenant_id": r.tenant_id,
        "key": r.key,
        "value": r.value,
        "created_at": _jsonify(r.created_at),
        "updated_at": _jsonify(r.updated_at),
    }


@router.put("/source-configs")
def upsert_source_config(body: SourceConfigIn) -> dict[str, Any]:
    key, value = body.key.strip(), body.value.strip()
    if not key or not value:
        raise HTTPException(status_code=422, detail="key and value must not be empty")
    stmt = (
        pg_insert(ConfigKV)
        .values(tenant_id=body.tenant_id, key=key, value=value)
        .on_conflict_do_update(
            constraint="uq_config_kv_tenant_key",
            set_={"value": value, "updated_at": func.now()},
        )
        .returning(ConfigKV)
    )
    with SessionLocal() as s:
        row = s.scalars(stmt).one()
        out = _source_config_dict(row)
        s.commit()
    return out


@router.get("/source-configs")
def list_source_configs(tenant_id: int = 1) -> list[dict[str, Any]]:
    with SessionLocal() as s:
        rows = s.scalars(
            select(ConfigKV)
            .where(ConfigKV.tenant_id == tenant_id)
            .order_by(ConfigKV.key)
        ).all()
        return [_source_config_dict(r) for r in rows]


@router.delete("/source-configs")
def delete_source_config(key: str, tenant_id: int = 1) -> dict[str, Any]:
    with SessionLocal() as s:
        n = s.execute(
            delete(ConfigKV)
            .where(ConfigKV.tenant_id == tenant_id)
            .where(ConfigKV.key == key)
        ).rowcount
        s.commit()
    if not n:
        raise HTTPException(status_code=404, detail="source config not found")
    return {"deleted": n}

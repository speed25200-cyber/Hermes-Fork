"""JEV (lecture) : sources, type d'événement, mapping, version, âge, disponibilité, délai, statut.

L'API ne voit ni la clé TypeSafe ni les documents bruts au-delà de leur titre : elle relit les tables
``source_documents``, ``document_versions``, ``jev_requests`` et ``jev_results``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import ApiContext, get_ctx
from okxq.api.readmodel import clamp
from okxq.api.serialize import as_iso, to_jsonable
from okxq.persistence.models import DocumentVersion, JevResult, SourceDocumentRow

router = APIRouter(tags=["jev"])


def _event_type(answers: dict[str, Any]) -> str | None:
    for key in ("event_type", "q_event_type", "type"):
        ans = answers.get(key)
        if isinstance(ans, dict):
            choice = ans.get("choice")
            if choice:
                return str(choice)
    return None


def _delay_ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


def jev_event_to_dict(
    now: datetime, r: JevResult, dv: DocumentVersion, sd: SourceDocumentRow
) -> dict[str, Any]:
    return {
        "evaluation_id": r.evaluation_id,
        "request_id": r.request_id,
        "source": sd.source,
        "document_id": sd.document_id,
        "document_version": dv.version,
        "title": dv.title,
        "language": sd.language,
        "published_at": as_iso(dv.published_at),
        "date_method": dv.date_method,
        "received_at": as_iso(dv.received_at),
        "event_type": _event_type(r.answers),
        "asset_mapping": to_jsonable(dv.asset_mapping),
        "asset_mapping_version": r.asset_mapping_version,
        "model_requested": r.model_version,
        "model_effective": r.model_effective,
        "question_hash": r.question_hash,
        "requested_at": as_iso(r.requested_at),
        "completed_at": as_iso(r.completed_at),
        "inference_completed_at": as_iso(r.inference_completed_at),
        "features_committed_at": as_iso(r.features_committed_at),
        "age_s": None if dv.published_at is None else int((now - dv.published_at).total_seconds()),
        "latency_ms": _delay_ms(r.requested_at, r.completed_at),
        "commit_delay_ms": _delay_ms(dv.received_at, r.features_committed_at),
        "status": r.status,
        "error": r.error,
        "usage": to_jsonable(r.usage),
        "answers": to_jsonable(r.answers),
    }


# Champs que SEUL le worker connaît (compteurs en mémoire, cache, budget dépensé). L'API ne les
# invente pas : elle les relaie depuis l'instantané que le worker écrit, ou les laisse à None.
WORKER_ONLY_FIELDS = (
    "cache_hit_ratio",
    "daily_spend_usd",
    "queue_length",
    "circuit_state",
    "tokens_input",
    "tokens_output",
    "latency_p50_ms",
    "latency_p95_ms",
    "available",
    "unavailable_reason",
)
# Au-delà de cet âge, l'instantané du worker ne décrit plus l'état courant : on ne le relaie pas.
STATUS_SNAPSHOT_MAX_AGE_SECONDS = 300


def _percentile(values: list[float], q: float) -> int | None:
    """Percentile par rang (interpolation linéaire). ``None`` sur une série vide : pas de zéro inventé."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return int(ordered[0])
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return int(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _worker_snapshot(ctx: ApiContext, now: datetime) -> dict[str, Any] | None:
    """Instantané JSON écrit par le worker JEV, s'il est lisible ET récent.

    Le worker tourne dans un AUTRE processus (et ne partage aucune mémoire avec l'API) : sans cet
    instantané, les compteurs qui lui appartiennent restent « non disponibles ». Un fichier illisible,
    mal formé ou périmé est traité comme absent — jamais comme un état sain.
    """
    raw = ctx.extra.get("jev_status_path")
    if not raw:
        return None
    path = Path(raw)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = now.timestamp() - path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or age > STATUS_SNAPSHOT_MAX_AGE_SECONDS:
        return None
    return payload


def jev_status_payload(ctx: ApiContext) -> dict[str, Any]:
    rm = ctx.readmodel
    now = ctx.clock.now_utc()
    counts = rm.jev_counts(now - timedelta(hours=24))
    results = rm.jev_results(limit=50)
    latencies = [
        (r.completed_at - r.requested_at).total_seconds() * 1000
        for r, _, _ in results
        if r.completed_at is not None
    ]
    evaluated = sum(v for k, v in counts.items() if k != "pending_requests")
    ok = counts.get("ok", 0)
    latest = results[0] if results else None
    # Les erreurs se comptent sur ce qui a été évalué : sans aucune évaluation, c'est une ABSENCE de
    # mesure (None), pas « zéro erreur ».
    errors = None if evaluated == 0 else evaluated - ok
    payload: dict[str, Any] = {
        "ok": True,
        "enabled": ctx.cfg.jev.enabled,
        "influence_mode": ctx.cfg.jev.influence_mode,
        "model_configured": ctx.cfg.jev.model,
        "model_effective_latest": None if latest is None else latest[0].model_effective,
        "asset_mapping_version_latest": None if latest is None else latest[0].asset_mapping_version,
        "sources": rm.jev_sources(),
        "counts_24h": counts,
        "availability_24h": None if evaluated == 0 else round(ok / evaluated, 4),
        "mean_latency_ms": None if not latencies else int(sum(latencies) / len(latencies)),
        "last_evaluation_at": None if latest is None else as_iso(latest[0].requested_at),
        "semantic_status": None if latest is None else latest[0].status,
        "budget_usd_per_day": ctx.cfg.jev.max_daily_spend_usd,
        "max_daily_spend_usd": ctx.cfg.jev.max_daily_spend_usd,
        "errors": errors,
        "evaluations_24h": evaluated,
        # Âge de la dernière évaluation aboutie : c'est ce que l'interface affiche.
        "age_seconds": None if latest is None else int((now - latest[0].requested_at).total_seconds()),
        "component": ctx.health.health_tick()["modules"].get("jev"),
        "status_source": "database",
        "ts": as_iso(now),
    }
    # Percentiles calculables depuis la base ; le worker les remplace par les siens s'il en publie.
    payload["latency_p50_ms"] = _percentile(latencies, 0.50)
    payload["latency_p95_ms"] = _percentile(latencies, 0.95)
    for field in WORKER_ONLY_FIELDS:
        payload.setdefault(field, None)
    snapshot = _worker_snapshot(ctx, now)
    if snapshot is not None:
        payload["status_source"] = "worker_snapshot"
        payload["snapshot_generated_at"] = snapshot.get("generated_at")
        for field in WORKER_ONLY_FIELDS:
            if field in snapshot:
                payload[field] = snapshot[field]
        # Le worker nomme sa dépense « spent_today_usd » ; l'interface lit « daily_spend_usd ».
        if payload.get("daily_spend_usd") is None and "spent_today_usd" in snapshot:
            payload["daily_spend_usd"] = snapshot["spent_today_usd"]
        reasons = snapshot.get("reasons")
        if payload.get("unavailable_reason") is None and isinstance(reasons, list) and reasons:
            payload["unavailable_reason"] = ", ".join(str(r) for r in reasons)
    elif not ctx.cfg.jev.enabled:
        # Désactivé par configuration : ce n'est pas une panne, et c'est une certitude, pas une absence.
        payload["available"] = False
        payload["unavailable_reason"] = "JEV_DESACTIVE_PAR_CONFIGURATION"
    return payload


@router.get("/api/v1/jev/status", summary="État JEV : sources, disponibilité, délais, statut sémantique")
def jev_status(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> dict[str, Any]:
    return jev_status_payload(get_ctx(request))


@router.get("/api/v1/jev/events", summary="Évaluations JEV récentes")
def jev_events(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    now = ctx.clock.now_utc()
    rows = ctx.readmodel.jev_results(limit=clamp(limit, 50))
    return {
        "ok": True,
        "count": len(rows),
        "items": [jev_event_to_dict(now, r, dv, sd) for r, dv, sd in rows],
    }

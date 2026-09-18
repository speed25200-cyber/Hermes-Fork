"""``/health/live`` (le processus répond) et ``/health/ready`` (réellement prêt). Sans authentification :
ce sont les sondes des conteneurs ; elles ne révèlent ni secret ni donnée de compte."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from okxq.api.context import get_ctx

router = APIRouter(tags=["santé"])


@router.get("/health/live", summary="Vivacité : le processus répond")
def live(request: Request) -> dict[str, Any]:
    return get_ctx(request).health.liveness()


@router.get("/health/ready", summary="Disponibilité réelle (503 si un composant requis n'est pas prêt)")
def ready(request: Request) -> JSONResponse:
    ok, detail = get_ctx(request).health.readiness()
    return JSONResponse(detail, status_code=200 if ok else 503)

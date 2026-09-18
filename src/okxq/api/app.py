"""Assemblage de l'API opérateur (§58) et service de l'interface conservée (§59).

Ce que ``create_app`` garantit par construction :

- **aucune route n'active LIVE** et aucune n'accepte « place cet ordre arbitraire » : la seule écriture
  est une demande opérateur auditée (``routes/control.py``) ;
- l'authentification est obligatoire dès que la configuration l'exige, avec un rôle par clé dérivée du
  secret serveur, une session signée courte, une protection CSRF sur les écritures et un CORS limité ;
- ``/health/live`` et ``/health/ready`` restent joignables sans clé (sondes d'orchestrateur), et rien
  d'autre ne l'est ;
- les métriques Prometheus ne sont exposées que si la configuration l'active, derrière la même porte.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from okxq import __version__
from okxq.api.auth import AuditTrail, AuthMiddleware, Role, SessionSigner, derive_role_keys
from okxq.api.context import ApiContext, get_ctx
from okxq.api.readmodel import ReadModel
from okxq.api.routes import account, compat, control, decisions, health, jev, orders, research, risk, system
from okxq.api.sse import EventBus, JournalPoller
from okxq.config.modes import Mode
from okxq.config.schema import AppConfig
from okxq.domain.clocks import Clock, SystemClock, ensure_utc
from okxq.domain.errors import ConfigError, OkxqError
from okxq.persistence.db import make_engine, make_session_factory
from okxq.runtime.alerts import AlertManager, build_default_manager
from okxq.runtime.health import DATABASE, HealthRegistry, Status
from okxq.runtime.logging import configure_logging, get_logger
from okxq.runtime.metrics import Metrics

__all__ = ["create_app"]

_log = get_logger("okxq.api")

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FRONTEND = ROOT / "frontend"

#: Composants dont l'état conditionne l'aptitude de CE processus (l'API n'envoie aucun ordre).
API_REQUIRED_COMPONENTS = frozenset({DATABASE})


def _session_factory(cfg: AppConfig) -> sessionmaker[Session]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # Sans base configurée, l'API démarre en lecture sur une base de session vide : elle dira
        # « Non disponible » partout plutôt que d'inventer des chiffres.
        url = "sqlite+pysqlite:///:memory:"
        _log.warning("DATABASE_URL absent : API en lecture sur une base vide (aucune donnée affichée)")
        engine = make_engine(url)
        from okxq.persistence.models import Base

        Base.metadata.create_all(engine)
        return make_session_factory(engine)
    if cfg.mode is Mode.LIVE and "live" not in url.lower():
        raise ConfigError("base LIVE et base non-LIVE ne se mélangent pas : nom de base incohérent")
    return make_session_factory(make_engine(url))


def create_app(
    cfg: AppConfig,
    *,
    session_factory: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
    frontend_dir: Path | None = None,
    operator_secret: str | None = None,
    alerts: AlertManager | None = None,
    synthetic_data: bool | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    the_clock = clock or SystemClock()
    if configure_logs:
        configure_logging(
            level=cfg.observability.log_level,
            json_output=cfg.observability.log_json,
            mode=cfg.mode.value,
        )
    factory = session_factory or _session_factory(cfg)
    secret = operator_secret if operator_secret is not None else os.environ.get("OPERATOR_AUTH_SECRET", "")
    if cfg.api.require_authentication and not secret:
        raise ConfigError(
            "OPERATOR_AUTH_SECRET absent : une API de pilotage joignable sans clé n'est pas une commodité"
        )
    readmodel = ReadModel(factory, cfg=cfg, clock=the_clock)
    registry = HealthRegistry(clock=the_clock, mode=cfg.mode.value, required=API_REQUIRED_COMPONENTS)
    bus = EventBus()
    ctx = ApiContext(
        cfg=cfg,
        session_factory=factory,
        readmodel=readmodel,
        health=registry,
        metrics=Metrics(),
        bus=bus,
        audit=AuditTrail(factory, bus=bus, clock=the_clock),
        clock=the_clock,
        alerts=alerts
        if alerts is not None
        else build_default_manager(
            sink_path=Path(cfg.storage.data_root) / "alerts" / "alerts.jsonl",
            alert_sink=cfg.observability.alert_sink,
            webhook_url=os.environ.get("ALERT_WEBHOOK_URL"),
            dedup_seconds=cfg.observability.alert_dedup_seconds,
            clock=the_clock,
        ),
        poller=JournalPoller(readmodel, bus),
        frontend_dir=frontend_dir
        if frontend_dir is not None
        else (DEFAULT_FRONTEND if cfg.api.serve_frontend else None),
        synthetic_data=bool(synthetic_data) if synthetic_data is not None else cfg.project.fixture_only,
        code_commit=os.environ.get("OKXQ_CODE_COMMIT"),
        started_at_iso=ensure_utc(the_clock.now_utc()).isoformat(),
        role_keys_present=bool(secret),
        extra={
            "data_root": cfg.storage.data_root,
            # Instantané écrit par le worker JEV (autre processus) : la seule façon pour l'API de
            # relayer des compteurs qui ne vivent que dans sa mémoire. Absent = « non disponible ».
            "jev_status_path": os.environ.get(
                "OKXQ_JEV_STATUS_PATH", str(Path(cfg.storage.data_root) / "jev" / "status.json")
            ),
        },
    )
    ok, detail = readmodel.db_ok()
    registry.set(DATABASE, Status.OK if ok else Status.FAULT, detail)

    app = FastAPI(
        title="okx-quant-jev — API opérateur",
        version=__version__,
        description=(
            "Lecture de l'état de la plateforme et demandes opérateur auditées. Aucune route n'active "
            "LIVE, ne place un ordre arbitraire, ni ne reçoit de clé d'exchange."
        ),
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    app.state.ctx = ctx
    # `require_role` cherche la piste d'audit sur `app.state.audit` pour consigner un refus de rôle.
    # Sans cette ligne, sa branche d'audit était protégée par `if audit is not None` et ne s'exécutait
    # jamais : un lecteur authentifié sondant une commande privilégiée était refusé SANS laisser de
    # trace, alors qu'un anonyme, refusé par le middleware, en laissait une. C'est l'inverse de ce
    # qu'on veut — un initié authentifié est le cas qui mérite le plus d'être consigné.
    app.state.audit = ctx.audit

    for module in (health, system, account, orders, decisions, risk, research, jev, control):
        app.include_router(module.router)
    # La compatibilité est montée EN DERNIER : sa route « /api/{canal} » est un attrape-tout.
    app.include_router(compat.router)

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        c = get_ctx(request)
        if not c.cfg.api.metrics_enabled:
            return JSONResponse({"ok": False, "error": "METRIQUES_DESACTIVEES"}, status_code=404)
        return PlainTextResponse(c.metrics.render().decode(), media_type="text/plain; version=0.0.4")

    if ctx.frontend_dir is not None and ctx.frontend_dir.exists():
        index = ctx.frontend_dir / "index.html"

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def page() -> Response:
            if not index.exists():
                return PlainTextResponse("interface absente", status_code=503)
            return FileResponse(
                index, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"}
            )

        app.mount("/", StaticFiles(directory=str(ctx.frontend_dir), html=False), name="frontend")
    else:

        @app.get("/", include_in_schema=False)
        def no_page() -> Response:
            return JSONResponse(
                {
                    "ok": True,
                    "message": "API sans interface servie",
                    "mode": cfg.mode.value,
                    "api": "/api/v1/docs",
                }
            )

    if cfg.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.api.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["content-type", "x-csrf-token"],
        )

    # Accès sans clé : traduit ici, une seule fois, du réglage vers un rôle. Le journal le dit fort,
    # au démarrage : une porte ouverte doit être une décision visible, pas une ligne de configuration
    # que personne ne relit.
    role_sans_cle = {"lecture": Role.READER, "total": Role.ADMIN}.get(cfg.api.acces_sans_cle)
    if role_sans_cle is not None:
        _log.warning(
            "acces_sans_cle",
            niveau=cfg.api.acces_sans_cle,
            role_accorde=role_sans_cle.value,
            mode=cfg.mode.value,
            avertissement=(
                "l'interface répond SANS clé ; toute personne qui atteint ce port obtient ce rôle"
            ),
        )

    # L'authentification est le middleware LE PLUS EXTERNE : elle voit toutes les requêtes.
    app.add_middleware(
        AuthMiddleware,
        keys=derive_role_keys(secret) if secret else {},
        signer=SessionSigner(secret or "no-secret", ttl_minutes=cfg.api.session_ttl_minutes),
        audit=ctx.audit,
        require_authentication=cfg.api.require_authentication,
        role_sans_cle=role_sans_cle,
    )

    @app.exception_handler(OkxqError)
    def domain_error(request: Request, exc: OkxqError) -> JSONResponse:
        _log.warning("erreur de domaine dans l'API", code=exc.code, path=request.url.path)
        return JSONResponse({"ok": False, "error": exc.code, "message": str(exc)}, status_code=400)

    return app


def app_factory() -> FastAPI:  # pragma: no cover - point d'entrée uvicorn
    """``uvicorn okxq.api.app:app_factory --factory`` : configuration lue dans OKXQ_CONFIG."""
    from okxq.config import load_config

    path = os.environ.get("OKXQ_CONFIG", "configs/paper.yaml")
    return create_app(load_config(path))


def role_key_for(secret: str, role: Role) -> str:
    """Clé d'accès d'un rôle (affichée une fois à l'opérateur, jamais journalisée)."""
    return derive_role_keys(secret)[role]


def principal_roles() -> tuple[str, ...]:
    return tuple(r.value for r in Role)

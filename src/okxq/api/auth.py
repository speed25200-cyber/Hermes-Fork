"""Authentification, rôles, sessions signées, CSRF et audit (§60, T65).

Mécanique conservée de l'interface Hermes : une clé dans l'URL (``?key=``) une seule fois, puis un
cookie. Ce qui change :

- la clé n'est plus le secret lui-même mais une clé DÉRIVÉE de ``OPERATOR_AUTH_SECRET`` par rôle
  (``reader`` / ``operator`` / ``admin``) : HMAC-SHA256(secret, "okxq-ui-key:v1:<rôle>") ;
- le cookie n'est pas la clé : c'est une session signée HMAC (rôle, expiration, jeton CSRF),
  ``HttpOnly``, ``SameSite=Lax``, durée ``api.session_ttl_minutes`` ;
- toute requête modifiante d'une session cookie exige le jeton CSRF en double soumission
  (cookie lisible ``okxq_csrf`` + en-tête ``X-CSRF-Token``, tous deux égaux au jeton signé) ;
- une requête non authentifiée, un rôle insuffisant ou un CSRF absent n'ont AUCUN effet et laissent
  un événement d'audit (``operator_actions`` avec statut ``DENIED``, journal, bus d'événements).

Aucune route n'active LIVE : ce module ne connaît pas de rôle qui le permette.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from okxq.domain.clocks import Clock, SystemClock
from okxq.domain.ids import new_id
from okxq.persistence.models import OperatorAction
from okxq.runtime.logging import get_logger

if TYPE_CHECKING:
    from okxq.api.sse import EventBus

COOKIE_SESSION = "okxq_session"
COOKIE_CSRF = "okxq_csrf"
HEADER_CSRF = "x-csrf-token"
QUERY_KEY = "key"
EXEMPT_PATHS: frozenset[str] = frozenset({"/health/live", "/health/ready"})
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
HTML_PAGES: frozenset[str] = frozenset({"/", "/index.html"})

log = get_logger("okxq.api.auth")


class Role(StrEnum):
    READER = "reader"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"reader": 0, "operator": 1, "admin": 2}[self.value]


@dataclass(frozen=True, slots=True)
class Principal:
    role: Role
    actor: str
    via: str  # "key" (clé dérivée présentée) ou "session" (cookie signé)

    def allows(self, minimum: Role) -> bool:
        return self.role.rank >= minimum.rank


def derive_role_keys(secret: str) -> dict[Role, str]:
    """Une clé distincte par rôle ; le secret lui-même n'est jamais présenté à un navigateur."""
    return {
        role: hmac.new(secret.encode("utf-8"), f"okxq-ui-key:v1:{role.value}".encode(), "sha256").hexdigest()
        for role in Role
    }


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass(frozen=True, slots=True)
class SessionClaims:
    role: Role
    csrf: str
    expires_at: datetime
    actor: str


class SessionSigner:
    """Sessions signées HMAC-SHA256 ; sans secret, aucune session n'est valide."""

    def __init__(self, secret: str | None, *, ttl_minutes: int, clock: Clock | None = None) -> None:
        self._secret = secret.encode("utf-8") if secret else None
        self._ttl = timedelta(minutes=ttl_minutes)
        self._clock = clock or SystemClock()

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def _sign(self, payload: str) -> str:
        assert self._secret is not None
        return hmac.new(self._secret, f"okxq-session:v1.{payload}".encode(), "sha256").hexdigest()

    def issue(self, role: Role, actor: str) -> tuple[str, str]:
        if self._secret is None:
            raise RuntimeError("OPERATOR_AUTH_SECRET absent : impossible d'émettre une session")
        now = self._clock.now_utc()
        csrf = secrets.token_urlsafe(24)
        body = {
            "r": role.value,
            "c": csrf,
            "a": actor,
            "i": int(now.timestamp()),
            "e": int((now + self._ttl).timestamp()),
            "n": secrets.token_hex(8),
        }
        payload = _b64e(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._sign(payload)}", csrf

    def verify(self, value: str | None) -> SessionClaims | None:
        if not value or self._secret is None or "." not in value:
            return None
        payload, _, signature = value.rpartition(".")
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            body = json.loads(_b64d(payload))
            role = Role(str(body["r"]))
            expires = datetime.fromtimestamp(int(body["e"]), tz=self._clock.now_utc().tzinfo)
            csrf = str(body["c"])
            actor = str(body.get("a", "session"))
        except (ValueError, KeyError, TypeError):
            return None
        if expires <= self._clock.now_utc():
            return None
        return SessionClaims(role=role, csrf=csrf, expires_at=expires, actor=actor)


class AuditTrail:
    """Trace toute action opérateur ET tout refus, en base (append), au journal et sur le bus."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._factory = session_factory
        self._bus = bus
        self._clock = clock or SystemClock()

    def record(
        self,
        *,
        action: str,
        scope: str,
        reason: str,
        actor: str,
        role: str,
        status: str,
        result: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str:
        rid = request_id or new_id("aud")
        now = self._clock.now_utc()
        if self._factory is not None:
            try:
                with self._factory() as session:
                    session.add(
                        OperatorAction(
                            request_id=rid,
                            action=action[:32],
                            scope=scope[:128],
                            reason=reason[:512],
                            actor=actor[:128],
                            role=role[:32],
                            requested_at=now,
                            status=status[:24],
                            observed_result=dict(result or {}),
                            completed_at=now if status == "DENIED" else None,
                        )
                    )
                    session.commit()
            except Exception as exc:  # l'audit ne doit jamais casser la réponse, mais il se voit
                log.error("audit_write_failed", action=action, error=type(exc).__name__)
        log.warning("operator_audit", action=action, scope=scope, actor=actor, role=role, status=status)
        if self._bus is not None:
            self._bus.publish(
                "ai-log",
                {
                    "ts": now.isoformat(),
                    "event": f"AUDIT_{status}",
                    "action": action,
                    "scope": scope,
                    "actor": actor,
                },
            )
        return rid


def _principal_from_key(keys: dict[Role, str], candidate: str | None) -> Principal | None:
    if not candidate:
        return None
    for role, key in keys.items():
        if hmac.compare_digest(key, candidate):
            return Principal(role=role, actor=f"{role.value}-key", via="key")
    return None


FORBIDDEN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes ✦ Quant</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#070708;color:#f0efec;font:14px/1.5 system-ui,sans-serif">
<form onsubmit="location='/?key='+encodeURIComponent(this.cle.value.trim());return false"
      style="text-align:center;padding:24px;max-width:340px">
  <div style="letter-spacing:.32em;font-weight:600;font-size:13px;margin-bottom:6px">HERMES <span style="color:#c9a254">&#10022;</span> QUANT</div>
  <p style="color:#a3a19b;font-size:12.5px;margin:0 0 18px">Cl&eacute; d&rsquo;acc&egrave;s requise.<br>Collez la cl&eacute; du tableau de bord&nbsp;&mdash; elle sera retenue sur cet appareil.</p>
  <input name="cle" autocomplete="off" autofocus placeholder="cl&eacute;"
    style="width:100%;box-sizing:border-box;padding:11px 13px;border-radius:9px;border:1px solid rgba(255,255,255,.14);background:#111112;color:#f0efec;font:13px ui-monospace,monospace;text-align:center;outline:none">
  <button style="margin-top:11px;width:100%;padding:11px;border-radius:9px;border:0;background:#c9a254;color:#171204;font:600 13.5px system-ui;cursor:pointer">Entrer</button>
</form></body></html>"""


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith("/api") or request.url.path in ("/metrics",):
        return False
    return "text/html" in request.headers.get("accept", "")


def forbidden_response(request: Request, error: str, message: str) -> Response:
    if _wants_html(request):
        return HTMLResponse(
            FORBIDDEN_PAGE,
            status_code=403,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'",
            },
        )
    return JSONResponse({"ok": False, "error": error, "message": message}, status_code=403)


def _cookie(name: str, value: str, *, max_age: int, http_only: bool, secure: bool) -> str:
    parts = [f"{name}={value}", f"Max-Age={max_age}", "Path=/", "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class AuthMiddleware:
    """ASGI pur : résout le principal, pose la session, applique CSRF, refuse et audite."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        keys: dict[Role, str],
        signer: SessionSigner,
        audit: AuditTrail,
        require_authentication: bool = True,
        role_sans_cle: Role | None = None,
    ) -> None:
        self.app = app
        self._keys = keys
        self._signer = signer
        self._audit = audit
        self._require = require_authentication
        #: Rôle accordé à une requête qui ne présente AUCUNE clé. ``None`` : la porte reste fermée.
        #: C'est le seul endroit où l'anonymat obtient des droits, et il est explicite.
        self._role_sans_cle = role_sans_cle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        principal: Principal | None = None
        set_cookies: list[str] = []
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

        presented = request.query_params.get(QUERY_KEY)
        bearer = request.headers.get("authorization", "")
        if not presented and bearer.lower().startswith("bearer "):
            presented = bearer[7:].strip()
        principal = _principal_from_key(self._keys, presented)
        if principal is not None:
            try:
                session_value, csrf = self._signer.issue(principal.role, principal.actor)
            except RuntimeError:
                principal = None
            else:
                ttl = self._signer.ttl_seconds
                set_cookies = [
                    _cookie(COOKIE_SESSION, session_value, max_age=ttl, http_only=True, secure=secure),
                    _cookie(COOKIE_CSRF, csrf, max_age=ttl, http_only=False, secure=secure),
                ]
        elif presented:
            self._audit.record(
                action="AUTH_KEY_REJECTED",
                scope=path,
                reason="clé présentée invalide",
                actor="anonymous",
                role="none",
                status="DENIED",
            )

        if principal is None:
            claims = self._signer.verify(request.cookies.get(COOKIE_SESSION))
            if claims is not None:
                principal = Principal(role=claims.role, actor=claims.actor, via="session")
                if request.method not in SAFE_METHODS:
                    header = request.headers.get(HEADER_CSRF, "")
                    cookie = request.cookies.get(COOKIE_CSRF, "")
                    if not (
                        header
                        and hmac.compare_digest(header, claims.csrf)
                        and hmac.compare_digest(cookie, claims.csrf)
                    ):
                        self._audit.record(
                            action="CSRF_REJECTED",
                            scope=f"{request.method} {path}",
                            reason="jeton CSRF absent ou différent",
                            actor=principal.actor,
                            role=principal.role.value,
                            status="DENIED",
                        )
                        response = forbidden_response(
                            request, "CSRF_REFUSE", "jeton CSRF absent ou invalide : aucune action effectuée"
                        )
                        await response(scope, receive, send)
                        return

        if principal is None and self._role_sans_cle is not None:
            # Accès libre demandé explicitement par l'exploitant. L'anonyme reçoit un RÔLE, et il est
            # nommé « anonyme » dans l'audit — jamais un acteur inventé qui laisserait croire à une
            # identité. Le rôle décide ensuite de ce qu'il peut faire : en `lecture`, les commandes
            # opérateur restent refusées par `require_role`, exactement comme pour une clé lecteur.
            principal = Principal(role=self._role_sans_cle, actor="anonyme", via="libre")

        if principal is None and self._require:
            if request.method not in SAFE_METHODS or path.startswith("/api"):
                self._audit.record(
                    action="AUTH_REQUIRED",
                    scope=f"{request.method} {path}",
                    reason="requête non authentifiée",
                    actor="anonymous",
                    role="none",
                    status="DENIED",
                )
            response = forbidden_response(request, "CLE_REFUSEE", "clé d'accès requise")
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})
        scope["state"]["principal"] = principal

        # Clé acceptée DANS L'URL sur une page : on redirige pour l'en retirer (historique, référents).
        #
        # La condition portait sur `set_cookies`, c'est-à-dire sur « une clé a été acceptée », sans
        # regarder d'OÙ elle venait. Une clé présentée par l'en-tête `Authorization` déclenchait donc
        # une redirection vers la même adresse — qui la représentait, qui redirigeait encore : boucle
        # infinie sur toute page HTML consultée avec un en-tête plutôt qu'un paramètre d'URL.
        #
        # Rediriger n'a de sens que s'il y a quelque chose à retirer. C'est la présence du paramètre
        # qui décide, pas le fait d'avoir authentifié.
        cle_dans_url = request.query_params.get(QUERY_KEY) is not None
        if set_cookies and cle_dans_url and request.method == "GET" and path in HTML_PAGES:
            params = [
                (k, v) for k, v in parse_qsl(request.url.query, keep_blank_values=True) if k != QUERY_KEY
            ]
            target = path + (f"?{urlencode(params)}" if params else "")
            response = RedirectResponse(target, status_code=303)
            for cookie in set_cookies:
                response.headers.append("set-cookie", cookie)
            await response(scope, receive, send)
            return

        if not set_cookies:
            await self.app(scope, receive, send)
            return

        async def send_with_cookies(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for cookie in set_cookies:
                    headers.append((b"set-cookie", cookie.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookies)


def current_principal(request: Request) -> Principal | None:
    state = request.scope.get("state") or {}
    p = state.get("principal")
    return p if isinstance(p, Principal) else None


def require_role(minimum: Role) -> Callable[[Request], Principal]:
    """Dépendance FastAPI : refuse (403 + audit) sous le rôle minimal ; aucun effet n'a lieu avant."""

    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if principal is None or not principal.allows(minimum):
            audit: AuditTrail | None = getattr(request.app.state, "audit", None)
            if audit is not None:
                audit.record(
                    action="ROLE_INSUFFICIENT",
                    scope=f"{request.method} {request.url.path}",
                    reason=f"rôle requis {minimum.value}",
                    actor=principal.actor if principal else "anonymous",
                    role=principal.role.value if principal else "none",
                    status="DENIED",
                )
            raise HTTPException(
                status_code=403,
                detail={
                    "ok": False,
                    "error": "ROLE_INSUFFISANT",
                    "message": f"cette action exige le rôle {minimum.value}",
                },
            )
        return principal

    return dependency

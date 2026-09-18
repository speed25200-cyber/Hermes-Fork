"""Client REST privé OKX v5 (§46) avec transport ``httpx`` injectable.

Garanties :
- ALLOWLIST stricte : seuls les endpoints nommés ci-dessous peuvent être appelés ; transferts, retraits,
  conversions, sous-comptes, changements de mode de compte ou de levier sont REFUSÉS avant tout réseau ;
- codes HTTP ET codes métier lus : ``code`` global de la réponse et ``sCode`` de chaque item (lot) ;
- timeouts séparés connexion / lecture / écriture / pool, plus un délai global (deadline) ;
- retry limité (avec jitter) uniquement sur les LECTURES sûres ; une écriture ambiguë (timeout, coupure,
  5xx, réponse illisible) lève ``ExchangeAmbiguousError`` → UNKNOWN puis réconciliation, jamais un renvoi ;
- DEMO : en-tête ``x-simulated-trading: 1`` sur CHAQUE requête ; le drapeau est immuable — un échec DEMO
  ne peut jamais produire une requête LIVE (T63).
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from okxq.domain.clocks import Clock
from okxq.domain.errors import ConfigError, ExchangeAmbiguousError, ExchangeError
from okxq.exchange.okx.authentication import OkxCredentials, okx_timestamp, rest_headers
from okxq.exchange.okx.rate_limits import BudgetClass, RateLimiter

__all__ = [
    "ALLOWLIST",
    "FORBIDDEN_PATH_PREFIXES",
    "Endpoint",
    "EndpointNotAllowedError",
    "OkxAuthError",
    "OkxPrivateRestClient",
    "OkxResponse",
    "OkxTimeouts",
    "RegionProfile",
]

log = structlog.get_logger(__name__)


class EndpointNotAllowedError(ExchangeError):
    code = "ENDPOINT_NOT_ALLOWED"


class OkxAuthError(ExchangeError):
    code = "EXCHANGE_AUTH"


@dataclass(frozen=True, slots=True)
class Endpoint:
    name: str
    method: str
    path: str
    family: str
    budget: BudgetClass
    scope_kind: str  # account / instrument
    safe_read: bool


def _ep(
    name: str, method: str, path: str, family: str, budget: BudgetClass, scope: str, safe: bool
) -> Endpoint:
    return Endpoint(name, method, path, family, budget, scope, safe)


ALLOWLIST: Mapping[str, Endpoint] = {
    e.name: e
    for e in (
        _ep(
            "account_config",
            "GET",
            "/api/v5/account/config",
            "account.config",
            BudgetClass.GENERAL,
            "account",
            True,
        ),
        _ep(
            "balance",
            "GET",
            "/api/v5/account/balance",
            "account.read",
            BudgetClass.RECONCILIATION,
            "account",
            True,
        ),
        _ep(
            "positions",
            "GET",
            "/api/v5/account/positions",
            "account.read",
            BudgetClass.RECONCILIATION,
            "account",
            True,
        ),
        _ep(
            "trade_fee",
            "GET",
            "/api/v5/account/trade-fee",
            "account.read",
            BudgetClass.GENERAL,
            "account",
            True,
        ),
        _ep(
            "leverage_info",
            "GET",
            "/api/v5/account/leverage-info",
            "account.read",
            BudgetClass.GENERAL,
            "account",
            True,
        ),
        _ep(
            "bills",
            "GET",
            "/api/v5/account/bills",
            "account.bills",
            BudgetClass.RECONCILIATION,
            "account",
            True,
        ),
        _ep(
            "orders_pending",
            "GET",
            "/api/v5/trade/orders-pending",
            "trade.read",
            BudgetClass.RECONCILIATION,
            "account",
            True,
        ),
        _ep("order", "GET", "/api/v5/trade/order", "trade.read", BudgetClass.RECONCILIATION, "account", True),
        _ep("fills", "GET", "/api/v5/trade/fills", "trade.read", BudgetClass.RECONCILIATION, "account", True),
        _ep(
            "fills_history",
            "GET",
            "/api/v5/trade/fills-history",
            "trade.fills_history",
            BudgetClass.RECONCILIATION,
            "account",
            True,
        ),
        _ep(
            "place_order",
            "POST",
            "/api/v5/trade/order",
            "trade.order",
            BudgetClass.TRADE,
            "instrument",
            False,
        ),
        _ep(
            "batch_orders",
            "POST",
            "/api/v5/trade/batch-orders",
            "trade.batch",
            BudgetClass.TRADE,
            "instrument",
            False,
        ),
        _ep(
            "cancel_order",
            "POST",
            "/api/v5/trade/cancel-order",
            "trade.order",
            BudgetClass.CANCEL,
            "instrument",
            False,
        ),
        _ep(
            "amend_order",
            "POST",
            "/api/v5/trade/amend-order",
            "trade.order",
            BudgetClass.TRADE,
            "instrument",
            False,
        ),
        _ep(
            "cancel_all_after",
            "POST",
            "/api/v5/trade/cancel-all-after",
            "trade.cancel_all_after",
            BudgetClass.HEARTBEAT,
            "account",
            False,
        ),
        _ep(
            "place_algo",
            "POST",
            "/api/v5/trade/order-algo",
            "trade.algo",
            BudgetClass.PROTECTION,
            "instrument",
            False,
        ),
        _ep(
            "cancel_algos",
            "POST",
            "/api/v5/trade/cancel-algos",
            "trade.algo",
            BudgetClass.PROTECTION,
            "instrument",
            False,
        ),
        _ep(
            "algo_pending",
            "GET",
            "/api/v5/trade/orders-algo-pending",
            "trade.read",
            BudgetClass.RECONCILIATION,
            "account",
            True,
        ),
        # Lecture publique (métadonnées d'instruments) : sans risque, budget téléchargement.
        _ep(
            "instruments",
            "GET",
            "/api/v5/public/instruments",
            "public.download",
            BudgetClass.DOWNLOAD,
            "ip",
            True,
        ),
    )
}

# Refusés explicitement, quel que soit l'appelant : jamais de mouvement de fonds ni de changement de compte.
FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v5/asset/",
    "/api/v5/finance/",
    "/api/v5/users/",
    "/api/v5/account/set-position-mode",
    "/api/v5/account/set-leverage",
    "/api/v5/account/set-account-level",
    "/api/v5/account/set-isolated-mode",
    "/api/v5/account/set-auto-loan",
    "/api/v5/account/set-greeks",
    "/api/v5/account/borrow-repay",
    "/api/v5/account/quick-margin-borrow-repay",
    "/api/v5/account/spot-manual-borrow-repay",
    "/api/v5/account/set-riskOffset-type",
    "/api/v5/account/activate-option",
    "/api/v5/account/set-fee-type",
    "/api/v5/copytrading/",
    "/api/v5/rfq/",
    "/api/v5/sprd/",
    "/api/v5/tradingBot/",
    "/api/v5/trade/mass-cancel",
    "/api/v5/trade/close-position",
)


@dataclass(frozen=True, slots=True)
class OkxTimeouts:
    connect: float = 2.0
    read: float = 5.0
    write: float = 5.0
    pool: float = 2.0
    deadline: float = 8.0

    def httpx(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.write, pool=self.pool)


@dataclass(frozen=True, slots=True)
class RegionProfile:
    """Domaines d'un profil de région (jamais une URL « universelle » pour contourner une restriction)."""

    name: str
    rest_base_url: str
    ws_private_url: str
    ws_private_demo_url: str

    def __post_init__(self) -> None:
        if not self.rest_base_url.startswith("https://"):
            raise ConfigError("rest_base_url doit être en HTTPS", profile=self.name)
        if not self.ws_private_url.startswith("wss://") or not self.ws_private_demo_url.startswith("wss://"):
            raise ConfigError("URL WebSocket doit être en WSS", profile=self.name)


@dataclass(frozen=True, slots=True)
class OkxResponse:
    http_status: int
    code: str
    msg: str
    data: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == "0"

    @property
    def partial(self) -> bool:
        """Lot partiellement réussi : ``code == "2"``, à traiter item par item via ``sCode`` (T37)."""
        return self.code == "2"


_RETRYABLE_READ_STATUS = {429, 500, 502, 503, 504}


class OkxPrivateRestClient:
    def __init__(
        self,
        *,
        credentials: OkxCredentials,
        profile: RegionProfile,
        demo: bool,
        clock: Clock,
        rate_limiter: RateLimiter,
        account_scope: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeouts: OkxTimeouts | None = None,
        max_read_retries: int = 2,
        rng: random.Random | None = None,
    ) -> None:
        self._creds = credentials
        self.profile = profile
        self._demo = bool(demo)
        self._clock = clock
        self._limiter = rate_limiter
        self.account_scope = account_scope
        self.timeouts = timeouts or OkxTimeouts()
        self._max_read_retries = max(0, max_read_retries)
        self._rng = rng or random.Random()  # jitter seulement ; pas de sécurité
        self._client = httpx.AsyncClient(
            base_url=profile.rest_base_url, transport=transport, timeout=self.timeouts.httpx()
        )
        self.requests_sent = 0

    @property
    def demo(self) -> bool:
        """Immuable : il n'existe aucune voie pour basculer un client DEMO en LIVE (T63)."""
        return self._demo

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- garde-fous ---------------------------------------------------------------------------------------

    @staticmethod
    def assert_path_allowed(method: str, path: str) -> Endpoint:
        for prefix in FORBIDDEN_PATH_PREFIXES:
            if path.startswith(prefix):
                raise EndpointNotAllowedError("endpoint interdit par construction", method=method, path=path)
        for ep in ALLOWLIST.values():
            if ep.method == method.upper() and ep.path == path:
                return ep
        raise EndpointNotAllowedError("endpoint hors allowlist", method=method, path=path)

    def endpoint(self, name: str) -> Endpoint:
        ep = ALLOWLIST.get(name)
        if ep is None:
            raise EndpointNotAllowedError("endpoint hors allowlist", name=name)
        return ep

    # --- requêtes ----------------------------------------------------------------------------------------

    async def request(
        self,
        name: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
        scope_id: str | None = None,
    ) -> OkxResponse:
        ep = self.endpoint(name)
        self.assert_path_allowed(ep.method, ep.path)
        if ep.method == "GET" and body is not None:
            raise ConfigError("un GET ne porte pas de body", endpoint=name)
        request_path = ep.path
        if params:
            request_path = f"{ep.path}?{urlencode(list(params.items()))}"
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        scope = scope_id or self.account_scope
        attempts = 1 + (self._max_read_retries if ep.safe_read else 0)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self._limiter.acquire(ep.family, scope, ep.budget)
            try:
                return await self._send_once(ep, request_path, body_text)
            except _RetryableRead as exc:
                last_exc = exc.cause
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (attempt + 1) + self._rng.random() * 0.05)
                    continue
                raise ExchangeError("lecture OKX en échec après retries", endpoint=name) from exc.cause
        raise ExchangeError("lecture OKX en échec", endpoint=name) from last_exc  # pragma: no cover

    async def _send_once(self, ep: Endpoint, request_path: str, body_text: str) -> OkxResponse:
        ts = okx_timestamp(self._clock.now_utc())
        headers = rest_headers(
            self._creds,
            timestamp=ts,
            method=ep.method,
            request_path=request_path,
            body=body_text,
            demo=self._demo,
        )
        self.requests_sent += 1
        try:
            async with asyncio.timeout(self.timeouts.deadline):
                response = await self._client.request(
                    ep.method,
                    request_path,
                    content=body_text.encode("utf-8") if body_text else None,
                    headers=headers,
                )
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as exc:
            if ep.safe_read:
                raise _RetryableRead(exc) from exc
            raise ExchangeAmbiguousError(
                "écriture OKX ambiguë (timeout/coupure) : réconcilier avant tout renvoi",
                endpoint=ep.name,
                error=str(exc),
            ) from exc
        return self._interpret(ep, response)

    def _interpret(self, ep: Endpoint, response: httpx.Response) -> OkxResponse:
        status = response.status_code
        if status in (401, 403):
            raise OkxAuthError("authentification OKX refusée", http_status=status, endpoint=ep.name)
        if status in _RETRYABLE_READ_STATUS and ep.safe_read:
            raise _RetryableRead(ExchangeError("réponse HTTP transitoire", http_status=status))
        try:
            payload = response.json()
        except ValueError as exc:
            if ep.safe_read:
                raise _RetryableRead(exc) from exc
            raise ExchangeAmbiguousError(
                "réponse OKX illisible", endpoint=ep.name, http_status=status
            ) from exc
        if not isinstance(payload, dict):
            raise ExchangeError("réponse OKX inattendue", endpoint=ep.name, http_status=status)
        code = str(payload.get("code", ""))
        msg = str(payload.get("msg", ""))
        data_raw = payload.get("data", [])
        data = [d for d in data_raw if isinstance(d, dict)] if isinstance(data_raw, list) else []
        if status >= 500:
            if ep.safe_read:
                raise _RetryableRead(ExchangeError("HTTP 5xx", http_status=status))
            raise ExchangeAmbiguousError("HTTP 5xx sur une écriture", endpoint=ep.name, http_status=status)
        if status == 429 or code == "50011":
            raise ExchangeError("limite de débit OKX atteinte", code="RATE_LIMITED", endpoint=ep.name)
        if status >= 400 and code == "":
            raise ExchangeError("erreur HTTP OKX", http_status=status, endpoint=ep.name)
        return OkxResponse(http_status=status, code=code, msg=msg, data=data, raw=payload)


class _RetryableRead(Exception):
    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def item_code(item: Mapping[str, Any]) -> tuple[str, str]:
    """Code et message d'un item de réponse (``sCode``/``sMsg``), ``"0"`` = succès."""
    return str(item.get("sCode", "0")), str(item.get("sMsg", ""))


def sleep_for(td: timedelta) -> float:  # pragma: no cover - aide triviale
    return max(0.0, td.total_seconds())

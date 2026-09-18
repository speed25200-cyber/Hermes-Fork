"""``OkxExchangeAdapter`` : implémentation OKX de ``okxq.exchange.base.ExchangeAdapter``.

- profil de région/domaine lu dans ``OKX_ACCOUNT_REGION_PROFILE`` (``global``, ``eea``, ``us``) : jamais
  une URL « universelle » choisie pour contourner une restriction ; profil absent = refus ;
- DEMO (``x-simulated-trading: 1``) et LIVE sont deux instances distinctes ; un échec DEMO ne bascule
  JAMAIS vers LIVE (le drapeau est fixé à la construction, T63) ;
- LIVE exige un ``LiveAuthorization`` vérifié AVANT toute connexion privée (T64) ;
- validation du compte au démarrage : ``posMode == net_mode`` ; l'adaptateur ne modifie JAMAIS un mode de
  compte ni un levier (ces endpoints sont interdits par construction dans le client REST).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from okxq.config.live_guard import LiveAuthorization
from okxq.config.modes import Mode
from okxq.config.schema import AppConfig
from okxq.domain.clocks import Clock
from okxq.domain.errors import ConfigError, ExchangeAmbiguousError, ExchangeError, LiveGuardError, OkxqError
from okxq.domain.events import Fill, Liquidity
from okxq.domain.instruments import InstrumentSpec, spec_from_okx_instrument
from okxq.domain.money import Side, dec
from okxq.exchange.base import (
    BalanceStatus,
    CancelResponse,
    ExchangeEvent,
    OrderRequest,
    OrderStatus,
    PlaceOutcome,
    PlaceResponse,
    PositionStatus,
    ProtectionRequest,
    ProtectionStatus,
)
from okxq.exchange.okx.authentication import OkxCredentials
from okxq.exchange.okx.rate_limits import DEFAULT_RULES, RateLimiter
from okxq.exchange.okx.rest_private import (
    OkxPrivateRestClient,
    OkxResponse,
    OkxTimeouts,
    RegionProfile,
    item_code,
)
from okxq.exchange.okx.websocket_private import (
    OkxPrivateWebSocket,
    WsConnector,
    parse_account_message,
    parse_algo_message,
    parse_position_message,
)
from okxq.persistence.repositories import build_execution_key

__all__ = ["REGION_PROFILES", "AccountValidation", "OkxExchangeAdapter", "region_profile_from_env"]

log = structlog.get_logger(__name__)

# Domaines par profil (docs-v5 « Overview » et sites régionaux, consultés le 2026-09-18). La validation
# connectée n'a pas été exécutée ici : l'opérateur confirme le profil de SON compte avant DEMO.
REGION_PROFILES: Mapping[str, RegionProfile] = {
    "global": RegionProfile(
        name="global",
        rest_base_url="https://www.okx.com",
        ws_private_url="wss://ws.okx.com:8443/ws/v5/private",
        ws_private_demo_url="wss://wspap.okx.com:8443/ws/v5/private",
    ),
    "eea": RegionProfile(
        name="eea",
        rest_base_url="https://eea.okx.com",
        ws_private_url="wss://wseea.okx.com:8443/ws/v5/private",
        ws_private_demo_url="wss://wseeapap.okx.com:8443/ws/v5/private",
    ),
    "us": RegionProfile(
        name="us",
        rest_base_url="https://app.okx.com",
        ws_private_url="wss://wsus.okx.com:8443/ws/v5/private",
        ws_private_demo_url="wss://wsuspap.okx.com:8443/ws/v5/private",
    ),
}

ORDER_NOT_FOUND_CODES = frozenset({"51603", "51000"})
_STATUS_STATE = {
    "live": "live",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "mmp_canceled": "canceled",
}


def region_profile_from_env(env: Mapping[str, str] | None = None) -> RegionProfile:
    src = os.environ if env is None else env
    name = (src.get("OKX_ACCOUNT_REGION_PROFILE") or "").strip().lower()
    if not name or name == "unspecified":
        raise ConfigError("OKX_ACCOUNT_REGION_PROFILE absent : aucun domaine universel par défaut")
    profile = REGION_PROFILES.get(name)
    if profile is None:
        raise ConfigError("profil de région OKX inconnu", profile=name, known=sorted(REGION_PROFILES))
    return profile


@dataclass(frozen=True, slots=True)
class AccountValidation:
    position_mode: str
    account_level: str
    uid: str
    checked_at: datetime
    ok: bool
    notes: tuple[str, ...]


def _ms(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


class OkxExchangeAdapter:
    """Adaptateur OKX (DEMO ou LIVE, jamais les deux)."""

    is_real_exchange = True

    def __init__(
        self,
        *,
        cfg: AppConfig,
        credentials: OkxCredentials,
        profile: RegionProfile,
        clock: Clock,
        ws_connector: WsConnector | None = None,
        transport: Any | None = None,
        rate_limiter: RateLimiter | None = None,
        timeouts: OkxTimeouts | None = None,
        live_authorization: LiveAuthorization | None = None,
        rest: OkxPrivateRestClient | None = None,
        ws: OkxPrivateWebSocket | None = None,
    ) -> None:
        mode = cfg.mode
        if mode not in (Mode.DEMO, Mode.LIVE):
            raise ConfigError("l'adaptateur OKX n'existe qu'en DEMO ou LIVE", mode=mode.value)
        if mode is Mode.LIVE:
            # T64 : refus AVANT toute connexion privée, sans exception ni option de contournement.
            if live_authorization is None or not live_authorization.covers(cfg):
                raise LiveGuardError("LIVE sans autorisation vérifiée : connexion privée refusée")
        self.cfg = cfg
        self._mode = mode
        self._demo = mode is Mode.DEMO
        self.profile = profile
        self._clock = clock
        self.account_scope = cfg.account.scope
        self.td_mode = cfg.account.margin_mode
        limiter = rate_limiter or RateLimiter(DEFAULT_RULES, clock=clock)
        self._rest = rest or OkxPrivateRestClient(
            credentials=credentials,
            profile=profile,
            demo=self._demo,
            clock=clock,
            rate_limiter=limiter,
            account_scope=self.account_scope,
            transport=transport,
            timeouts=timeouts,
        )
        if self._rest.demo != self._demo:
            raise ConfigError("client REST incohérent avec le mode de l'adaptateur")
        # Instruments écartés à la lecture des métadonnées, comptés par motif : exposé dans la santé,
        # jamais silencieux (une divergence de contrat fournisseur doit se voir).
        self._skipped_instruments: dict[str, int] = {}
        if ws is None:
            if ws_connector is None:
                raise ConfigError("un connecteur WebSocket est requis (aucune socket implicite)")
            ws = OkxPrivateWebSocket(
                credentials=credentials,
                url=profile.ws_private_demo_url if self._demo else profile.ws_private_url,
                connector=ws_connector,
                clock=clock,
                account_scope=self.account_scope,
            )
        self._ws = ws
        self.validation: AccountValidation | None = None

    # --- propriétés -----------------------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "okx-demo" if self._demo else "okx-live"

    @property
    def demo(self) -> bool:
        return self._demo

    @property
    def rest(self) -> OkxPrivateRestClient:
        return self._rest

    # --- démarrage ---------------------------------------------------------------------------------------

    async def validate_account(self) -> AccountValidation:
        """Lit la configuration du compte ; refuse si le mode n'est pas ``net_mode``. Ne modifie rien."""
        resp = await self._rest.request("account_config")
        if not resp.ok or not resp.data:
            raise ExchangeError("configuration de compte illisible", code="ACCOUNT_CONFIG", msg=resp.msg)
        cfg = resp.data[0]
        pos_mode = str(cfg.get("posMode", ""))
        notes: list[str] = []
        ok = True
        if pos_mode != "net_mode":
            ok = False
            notes.append(f"posMode={pos_mode!r} attendu 'net_mode' : l'adaptateur ne change JAMAIS le mode")
        validation = AccountValidation(
            position_mode=pos_mode,
            account_level=str(cfg.get("acctLv", "")),
            uid=str(cfg.get("uid", "")),
            checked_at=self._clock.now_utc(),
            ok=ok,
            notes=tuple(notes),
        )
        self.validation = validation
        if not ok:
            raise ExchangeError("compte OKX non conforme", code="ACCOUNT_MODE_MISMATCH", notes=notes)
        return validation

    async def connect(self) -> None:
        await self.validate_account()
        await self._ws.connect()

    async def close(self) -> None:
        await self._ws.close()
        await self._rest.aclose()

    # --- instruments -----------------------------------------------------------------------------------

    async def instruments(self) -> list[InstrumentSpec]:
        resp = await self._rest.request("instruments", params={"instType": "SWAP"})
        now = self._clock.now_utc()
        specs: list[InstrumentSpec] = []
        for item in resp.data:
            try:
                specs.append(
                    spec_from_okx_instrument(item, observed_at=now, provenance=f"okx:{self.profile.name}")
                )
            except (ExchangeError, OkxqError, ValueError) as exc:
                # Instruments non supportés (inverse, options, ctMult≠1…) : ignorés, jamais convertis.
                # Le motif est compté et nommé : un rejet silencieux masquerait une divergence de contrat.
                self._skipped_instruments[type(exc).__name__] = (
                    self._skipped_instruments.get(type(exc).__name__, 0) + 1
                )
                continue
        return specs

    # --- ordres ------------------------------------------------------------------------------------------

    def _order_body(self, request: OrderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": request.inst_id,
            "tdMode": self.td_mode,
            "clOrdId": request.client_order_id,
            "side": request.side,
            "ordType": request.order_type,
            "sz": format(request.contracts, "f"),
            "reduceOnly": bool(request.reduce_only),
            "tag": "okxq",
        }
        if request.order_type != "market":
            if request.price_limit is None:
                raise ConfigError("ordre à prix limité sans prix", client_order_id=request.client_order_id)
            body["px"] = format(request.price_limit, "f")
        return body

    async def place_order(self, request: OrderRequest) -> PlaceResponse:
        sent_at = self._clock.now_utc()
        body = self._order_body(request)
        try:
            resp = await self._rest.request("place_order", body=body, scope_id=request.inst_id)
        except ExchangeAmbiguousError as exc:
            return PlaceResponse(
                outcome=PlaceOutcome.UNKNOWN,
                client_order_id=request.client_order_id,
                code=exc.code,
                message=str(exc),
                sent_at=sent_at,
            )
        except ExchangeError as exc:
            return PlaceResponse(
                outcome=PlaceOutcome.REJECTED,
                client_order_id=request.client_order_id,
                code=exc.code,
                message=str(exc),
                sent_at=sent_at,
            )
        return self._interpret_place(resp, request.client_order_id, sent_at)

    def _interpret_place(self, resp: OkxResponse, client_order_id: str, sent_at: datetime) -> PlaceResponse:
        item = resp.data[0] if resp.data else {}
        s_code, s_msg = item_code(item)
        if resp.ok and s_code == "0":
            return PlaceResponse(
                outcome=PlaceOutcome.ACK,
                client_order_id=client_order_id,
                exchange_order_id=str(item.get("ordId", "")) or None,
                code="0",
                message=s_msg or resp.msg,
                sent_at=sent_at,
                ack_at=self._clock.now_utc(),
            )
        code = s_code if s_code != "0" else (resp.code or "unknown")
        return PlaceResponse(
            outcome=PlaceOutcome.REJECTED,
            client_order_id=client_order_id,
            code=code,
            message=s_msg or resp.msg,
            sent_at=sent_at,
        )

    async def place_orders_batch(self, requests: list[OrderRequest]) -> list[PlaceResponse]:
        """Lot OKX : réponse lue ITEM PAR ITEM (``code == "2"`` = succès partiel, T37)."""
        if not requests:
            return []
        sent_at = self._clock.now_utc()
        inst = requests[0].inst_id
        try:
            resp = await self._rest.request(
                "batch_orders", body=[self._order_body(r) for r in requests], scope_id=inst
            )
        except ExchangeAmbiguousError as exc:
            return [
                PlaceResponse(
                    PlaceOutcome.UNKNOWN, r.client_order_id, code=exc.code, message=str(exc), sent_at=sent_at
                )
                for r in requests
            ]
        by_cl = {str(d.get("clOrdId", "")): d for d in resp.data}
        out: list[PlaceResponse] = []
        for r in requests:
            item = by_cl.get(r.client_order_id)
            if item is None:
                out.append(
                    PlaceResponse(
                        PlaceOutcome.UNKNOWN,
                        r.client_order_id,
                        code=resp.code,
                        message="item absent",
                        sent_at=sent_at,
                    )
                )
                continue
            out.append(
                self._interpret_place(
                    OkxResponse(
                        resp.http_status, "0" if item_code(item)[0] == "0" else resp.code, resp.msg, [item]
                    ),
                    r.client_order_id,
                    sent_at,
                )
            )
        return out

    async def cancel_order(self, account_scope: str, client_order_id: str, inst_id: str) -> CancelResponse:
        self._check_scope(account_scope)
        try:
            resp = await self._rest.request(
                "cancel_order", body={"instId": inst_id, "clOrdId": client_order_id}, scope_id=inst_id
            )
        except ExchangeAmbiguousError as exc:
            return CancelResponse(PlaceOutcome.UNKNOWN, client_order_id, code=exc.code, message=str(exc))
        except ExchangeError as exc:
            return CancelResponse(PlaceOutcome.REJECTED, client_order_id, code=exc.code, message=str(exc))
        item = resp.data[0] if resp.data else {}
        s_code, s_msg = item_code(item)
        if resp.ok and s_code == "0":
            return CancelResponse(PlaceOutcome.ACK, client_order_id, code="0", message=s_msg)
        return CancelResponse(
            PlaceOutcome.REJECTED,
            client_order_id,
            code=s_code if s_code != "0" else resp.code,
            message=s_msg or resp.msg,
        )

    async def cancel_all_after(self, timeout_seconds: int) -> bool:
        try:
            resp = await self._rest.request("cancel_all_after", body={"timeOut": str(int(timeout_seconds))})
        except ExchangeError as exc:
            log.warning("cancel_all_after_error", error=str(exc))
            return False
        return resp.ok

    async def get_order(self, account_scope: str, client_order_id: str, inst_id: str) -> OrderStatus | None:
        self._check_scope(account_scope)
        resp = await self._rest.request("order", params={"instId": inst_id, "clOrdId": client_order_id})
        if not resp.ok:
            if resp.code in ORDER_NOT_FOUND_CODES:
                return None
            raise ExchangeError("détail d'ordre en erreur", code=resp.code, msg=resp.msg)
        if not resp.data:
            return None
        return self._order_status(resp.data[0])

    def _order_status(self, item: dict[str, Any]) -> OrderStatus:
        avg = item.get("avgPx")
        return OrderStatus(
            client_order_id=str(item.get("clOrdId", "")),
            exchange_order_id=str(item.get("ordId", "")) or None,
            state=_STATUS_STATE.get(str(item.get("state", "")), "unknown"),
            filled_contracts=dec(str(item.get("accFillSz") or "0"), field="accFillSz"),
            average_price=None if avg in (None, "") else dec(str(avg), field="avgPx"),
            updated_at=_ms(item.get("uTime")),
            raw=dict(item),
        )

    async def open_orders(self, account_scope: str) -> list[OrderStatus]:
        self._check_scope(account_scope)
        resp = await self._rest.request("orders_pending", params={"instType": "SWAP"})
        return [self._order_status(d) for d in resp.data]

    async def fills_since(self, account_scope: str, since: datetime) -> list[Fill]:
        self._check_scope(account_scope)
        now = self._clock.now_utc()
        name = "fills" if now - since <= timedelta(days=3) else "fills_history"
        begin = str(int(since.timestamp() * 1000))
        fills: list[Fill] = []
        after: str | None = None
        for _ in range(10):  # pages de 100, bornées
            params = {"instType": "SWAP", "begin": begin}
            if after:
                params["after"] = after
            resp = await self._rest.request(name, params=params)
            for item in resp.data:
                fill = self._fill_from_item(item, now)
                if fill is not None:
                    fills.append(fill)
            if len(resp.data) < 100:
                break
            after = str(resp.data[-1].get("billId", "")) or None
            if not after:
                break
        return fills

    def _fill_from_item(self, item: dict[str, Any], receive_ts: datetime) -> Fill | None:
        ord_id = str(item.get("ordId", "")) or None
        trade_id = str(item.get("tradeId", "")) or None
        cl = str(item.get("clOrdId", "")) or None
        if not (ord_id and trade_id and cl):
            return None
        exec_type = str(item.get("execType", ""))
        return Fill(
            execution_key=build_execution_key(self.account_scope, str(item["instId"]), ord_id, trade_id),
            account_scope=self.account_scope,
            inst_id=str(item["instId"]),
            client_order_id=cl,
            exchange_order_id=ord_id,
            trade_id=trade_id,
            side=Side(str(item["side"])),
            contracts=dec(str(item.get("fillSz") or "0"), field="fillSz"),
            fill_price=dec(str(item.get("fillPx") or "0"), field="fillPx"),
            fee_cashflow=dec(str(item.get("fee") or "0"), field="fee"),
            fee_ccy=str(item.get("feeCcy") or "USDT"),
            fill_at=_ms(item.get("ts")) or receive_ts,
            receive_ts=receive_ts,
            liquidity=Liquidity.MAKER
            if exec_type == "M"
            else Liquidity.TAKER
            if exec_type == "T"
            else Liquidity.UNKNOWN,
        )

    # --- compte -------------------------------------------------------------------------------------------

    async def positions(self, account_scope: str) -> list[PositionStatus]:
        self._check_scope(account_scope)
        resp = await self._rest.request("positions", params={"instType": "SWAP"})
        now = self._clock.now_utc()
        return [
            parse_position_message(d, receive_ts=now)
            for d in resp.data
            if str(d.get("pos", "0")) not in ("", "0")
        ]

    async def balance(self, account_scope: str) -> BalanceStatus:
        self._check_scope(account_scope)
        resp = await self._rest.request("balance", params={"ccy": "USDT"})
        if not resp.data:
            raise ExchangeError("solde illisible", code=resp.code, msg=resp.msg)
        return parse_account_message(resp.data[0], receive_ts=self._clock.now_utc())

    # --- protections -----------------------------------------------------------------------------------

    async def place_protection(self, request: ProtectionRequest) -> ProtectionStatus:
        self._check_scope(request.account_scope)
        body = {
            "instId": request.inst_id,
            "tdMode": self.td_mode,
            "side": request.side,
            "ordType": "conditional",
            "sz": format(request.contracts, "f"),
            "reduceOnly": True,
            "algoClOrdId": request.client_algo_id,
            "slTriggerPx": format(request.trigger_price, "f"),
            "slOrdPx": "-1",
            "slTriggerPxType": request.trigger_reference,
        }
        now = self._clock.now_utc()
        try:
            resp = await self._rest.request("place_algo", body=body, scope_id=request.inst_id)
        except ExchangeAmbiguousError:
            return ProtectionStatus(
                request.client_algo_id, None, "unknown", request.trigger_price, request.contracts, now
            )
        except ExchangeError as exc:
            return ProtectionStatus(
                request.client_algo_id,
                None,
                "rejected",
                request.trigger_price,
                request.contracts,
                now,
                raw={"error": str(exc)},
            )
        item = resp.data[0] if resp.data else {}
        s_code, s_msg = item_code(item)
        state = "live" if resp.ok and s_code == "0" else "rejected"
        return ProtectionStatus(
            client_algo_id=request.client_algo_id,
            exchange_algo_id=str(item.get("algoId", "")) or None,
            state=state,
            trigger_price=request.trigger_price,
            contracts=request.contracts,
            as_of=now,
            raw={"sCode": s_code, "sMsg": s_msg, "code": resp.code},
        )

    async def cancel_protection(
        self, account_scope: str, client_algo_id: str, inst_id: str
    ) -> ProtectionStatus:
        self._check_scope(account_scope)
        now = self._clock.now_utc()
        try:
            resp = await self._rest.request(
                "cancel_algos", body=[{"instId": inst_id, "algoClOrdId": client_algo_id}], scope_id=inst_id
            )
        except ExchangeAmbiguousError:
            return ProtectionStatus(client_algo_id, None, "unknown", None, None, now)
        item = resp.data[0] if resp.data else {}
        s_code, _ = item_code(item)
        return ProtectionStatus(
            client_algo_id,
            str(item.get("algoId", "")) or None,
            "canceled" if s_code == "0" else "unknown",
            None,
            None,
            now,
        )

    async def protections(self, account_scope: str) -> list[ProtectionStatus]:
        self._check_scope(account_scope)
        resp = await self._rest.request("algo_pending", params={"ordType": "conditional", "instType": "SWAP"})
        now = self._clock.now_utc()
        return [parse_algo_message(d, receive_ts=now) for d in resp.data]

    # --- flux privé --------------------------------------------------------------------------------------

    async def events(self) -> AsyncIterator[ExchangeEvent]:
        if not self._ws.logged_in:
            await self._ws.connect()
        while True:
            try:
                batch = await self._ws.read_once()
            except ExchangeError:
                raise
            except Exception as exc:
                yield ExchangeEvent(
                    kind="disconnect", receive_ts=self._clock.now_utc(), raw={"error": str(exc)}
                )
                await self._ws.reconnect()
                continue
            for event in batch:
                yield event

    # --- internes ----------------------------------------------------------------------------------------

    def _check_scope(self, account_scope: str) -> None:
        if account_scope != self.account_scope:
            raise ConfigError("portée de compte inattendue", expected=self.account_scope, got=account_scope)


def demo_safety_invariant(adapter: OkxExchangeAdapter) -> bool:
    """T63 : un adaptateur DEMO reste DEMO (client REST et adaptateur), quoi qu'il arrive."""
    return adapter.demo == adapter.rest.demo and (adapter.cfg.mode is Mode.DEMO) == adapter.demo


_ = Decimal  # unité de quantité conservée pour les signatures futures (évite un import inutile signalé)

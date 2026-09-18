"""WebSocket privé OKX v5 : login, canaux ``orders`` / ``positions`` / ``account`` / ``orders-algo``,
ping, reconnexion bornée avec jitter, transport injectable (aucune socket ouverte par ce module).

Les messages sont traduits en ``ExchangeEvent`` (contrats internes). Un message d'ordre porte un
``raw_hash`` stable : une relivraison est dédupliquée en aval (T34). Un fill (``fillSz > 0`` avec
``tradeId``) produit aussi un ``Fill`` à clé ``{account_scope}:{instId}:{ordId}:{tradeId}``.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog

from okxq.domain.clocks import Clock
from okxq.domain.errors import ExchangeError
from okxq.domain.events import Fill, Liquidity, OrderEvent, OrderEventKind
from okxq.domain.ids import payload_hash
from okxq.domain.money import Side, dec
from okxq.domain.orders import OrderState
from okxq.exchange.base import BalanceStatus, ExchangeEvent, PositionStatus, ProtectionStatus
from okxq.exchange.okx.authentication import OkxCredentials, ws_login_args
from okxq.persistence.repositories import build_execution_key

__all__ = [
    "OkxPrivateWebSocket",
    "WsConnector",
    "WsTransport",
    "parse_account_message",
    "parse_algo_message",
    "parse_order_message",
    "parse_position_message",
]

log = structlog.get_logger(__name__)

EventSink = Callable[[ExchangeEvent], Awaitable[None]]


class WsTransport(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


WsConnector = Callable[[str], Awaitable[WsTransport]]

_OKX_STATE: dict[str, OrderState] = {
    "live": OrderState.ACKNOWLEDGED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "mmp_canceled": OrderState.CANCELED,
}


def _ms(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


def _d(value: Any, field: str, default: str = "0") -> Decimal:
    return dec(str(value) if value not in (None, "") else default, field=field)


def parse_order_message(
    item: dict[str, Any], *, account_scope: str, receive_ts: datetime
) -> tuple[OrderEvent | None, Fill | None]:
    """Traduit un item du canal ``orders``. Retourne (événement, fill éventuel)."""
    cl_ord_id = str(item.get("clOrdId", ""))
    if not cl_ord_id:
        return None, None
    okx_state = str(item.get("state", ""))
    state = _OKX_STATE.get(okx_state)
    if state is None:
        return None, None
    fill_sz = _d(item.get("fillSz"), "fillSz")
    acc_fill = _d(item.get("accFillSz"), "accFillSz")
    if fill_sz > 0:
        kind = OrderEventKind.FILL if state is OrderState.FILLED else OrderEventKind.PARTIAL_FILL
    elif state is OrderState.CANCELED:
        kind = OrderEventKind.CANCEL
    elif str(item.get("amendResult", "")) not in ("", "-1"):
        kind = OrderEventKind.AMEND
    else:
        kind = OrderEventKind.ACK
    avg_px = item.get("avgPx")
    event = OrderEvent(
        event_id=f"okx-{cl_ord_id}-{payload_hash(item)[:16]}",
        client_order_id=cl_ord_id,
        exchange_order_id=str(item.get("ordId")) or None,
        event_kind=kind,
        observed_state=state,
        cumulative_filled=acc_fill,
        average_fill_price=None if avg_px in (None, "") else _d(avg_px, "avgPx"),
        event_ts=_ms(item.get("uTime")) or _ms(item.get("cTime")),
        receive_ts=receive_ts,
        raw_hash=payload_hash(item),
        reason=str(item.get("cancelSource", "")) or None,
    )
    fill: Fill | None = None
    trade_id = str(item.get("tradeId", "")) or None
    ord_id = str(item.get("ordId", "")) or None
    if fill_sz > 0 and trade_id and ord_id:
        exec_type = str(item.get("execType", ""))
        fill = Fill(
            execution_key=build_execution_key(account_scope, str(item["instId"]), ord_id, trade_id),
            account_scope=account_scope,
            inst_id=str(item["instId"]),
            client_order_id=cl_ord_id,
            exchange_order_id=ord_id,
            trade_id=trade_id,
            side=Side(str(item["side"])),
            contracts=fill_sz,
            fill_price=_d(item.get("fillPx"), "fillPx"),
            fee_cashflow=_d(item.get("fillFee"), "fillFee"),
            fee_ccy=str(item.get("fillFeeCcy") or "USDT"),
            fill_at=_ms(item.get("fillTime")) or receive_ts,
            receive_ts=receive_ts,
            liquidity=Liquidity.MAKER
            if exec_type == "M"
            else Liquidity.TAKER
            if exec_type == "T"
            else Liquidity.UNKNOWN,
        )
    return event, fill


def parse_position_message(item: dict[str, Any], *, receive_ts: datetime) -> PositionStatus:
    lever = item.get("lever")
    return PositionStatus(
        inst_id=str(item["instId"]),
        signed_contracts=_d(item.get("pos"), "pos"),
        average_price=_d(item.get("avgPx"), "avgPx"),
        mark_price=None if item.get("markPx") in (None, "") else _d(item.get("markPx"), "markPx"),
        liquidation_price=None if item.get("liqPx") in (None, "") else _d(item.get("liqPx"), "liqPx"),
        margin=None if item.get("margin") in (None, "") else _d(item.get("margin"), "margin"),
        leverage=None if lever in (None, "") else _d(lever, "lever"),
        as_of=_ms(item.get("uTime")) or receive_ts,
        raw=dict(item),
    )


def parse_account_message(
    item: dict[str, Any], *, receive_ts: datetime, settle_ccy: str = "USDT"
) -> BalanceStatus:
    details = {str(d.get("ccy")): d for d in item.get("details", []) if isinstance(d, dict)}
    usdt = details.get(settle_ccy, {})
    return BalanceStatus(
        settle_ccy=settle_ccy,
        total_equity=_d(usdt.get("eq") or item.get("totalEq"), "eq"),
        cash_balance=_d(usdt.get("cashBal"), "cashBal"),
        available=_d(usdt.get("availEq") or usdt.get("availBal"), "availEq"),
        used_margin=None if usdt.get("frozenBal") in (None, "") else _d(usdt.get("frozenBal"), "frozenBal"),
        unrealized_pnl=None if usdt.get("upl") in (None, "") else _d(usdt.get("upl"), "upl"),
        as_of=_ms(item.get("uTime")) or receive_ts,
        raw=dict(item),
    )


def parse_algo_message(item: dict[str, Any], *, receive_ts: datetime) -> ProtectionStatus:
    state_map = {"live": "live", "effective": "triggered", "canceled": "canceled", "order_failed": "rejected"}
    trig = item.get("slTriggerPx") or item.get("triggerPx")
    return ProtectionStatus(
        client_algo_id=str(item.get("algoClOrdId", "")),
        exchange_algo_id=str(item.get("algoId", "")) or None,
        state=state_map.get(str(item.get("state", "")), "unknown"),
        trigger_price=None if trig in (None, "") else _d(trig, "triggerPx"),
        contracts=None if item.get("sz") in (None, "") else _d(item.get("sz"), "sz"),
        as_of=_ms(item.get("uTime")) or receive_ts,
        raw=dict(item),
    )


class OkxPrivateWebSocket:
    def __init__(
        self,
        *,
        credentials: OkxCredentials,
        url: str,
        connector: WsConnector,
        clock: Clock,
        account_scope: str,
        ping_interval: timedelta = timedelta(seconds=25),
        max_reconnect_attempts: int = 5,
        backoff_base: timedelta = timedelta(seconds=1),
        backoff_cap: timedelta = timedelta(seconds=30),
        rng: random.Random | None = None,
        inst_type: str = "SWAP",
    ) -> None:
        self._creds = credentials
        self.url = url
        self._connector = connector
        self._clock = clock
        self.account_scope = account_scope
        self.ping_interval = ping_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._rng = rng or random.Random()
        self.inst_type = inst_type
        self._transport: WsTransport | None = None
        self._queue: asyncio.Queue[ExchangeEvent] = asyncio.Queue()
        self.reconnects = 0
        self.logged_in = False
        self.sent_messages: list[dict[str, Any]] = []

    # --- connexion ---------------------------------------------------------------------------------------

    async def connect(self) -> None:
        self._transport = await self._connector(self.url)
        args = ws_login_args(self._creds, epoch_seconds=int(self._clock.now_utc().timestamp()))
        await self._send({"op": "login", "args": [args]})
        reply = json.loads(await self._transport.recv())
        if reply.get("event") != "login" or str(reply.get("code", "")) != "0":
            await self._transport.close()
            raise ExchangeError("login WebSocket refusé", code="EXCHANGE_AUTH", reply=str(reply)[:200])
        self.logged_in = True
        await self._send(
            {
                "op": "subscribe",
                "args": [
                    {"channel": "orders", "instType": self.inst_type},
                    {"channel": "positions", "instType": self.inst_type},
                    {"channel": "account"},
                    {"channel": "orders-algo", "instType": self.inst_type},
                ],
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._transport is not None
        self.sent_messages.append(message)
        await self._transport.send(json.dumps(message, separators=(",", ":")))

    def backoff(self, attempt: int) -> timedelta:
        base: timedelta = min(self.backoff_cap, self.backoff_base * (2**attempt))
        jitter = base.total_seconds() * self._rng.uniform(0.0, 0.5)
        return timedelta(seconds=base.total_seconds() + jitter)

    async def reconnect(self) -> None:
        """Reconnexion bornée avec backoff exponentiel et jitter ; échec définitif = ``ExchangeError``."""
        self.logged_in = False
        for attempt in range(self.max_reconnect_attempts):
            await asyncio.sleep(self.backoff(attempt).total_seconds())
            try:
                await self.connect()
                self.reconnects += 1
                return
            except Exception as exc:
                log.warning("ws_reconnect_failed", attempt=attempt + 1, error=str(exc))
        raise ExchangeError("reconnexion WebSocket abandonnée", attempts=self.max_reconnect_attempts)

    async def close(self) -> None:
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        self.logged_in = False

    # --- boucle de lecture -------------------------------------------------------------------------------

    async def read_once(self) -> list[ExchangeEvent]:
        """Lit un message (ou envoie un ping si rien n'arrive) et retourne les événements traduits."""
        assert self._transport is not None
        try:
            text = await asyncio.wait_for(self._transport.recv(), timeout=self.ping_interval.total_seconds())
        except TimeoutError:
            await self._transport.send("ping")
            return [ExchangeEvent(kind="heartbeat", receive_ts=self._clock.now_utc())]
        return self.translate(text)

    def translate(self, text: str) -> list[ExchangeEvent]:
        receive_ts = self._clock.now_utc()
        if text == "pong":
            return []
        try:
            message = json.loads(text)
        except ValueError:
            log.warning("ws_message_unparseable", head=text[:80])
            return []
        if not isinstance(message, dict):
            return []
        if "event" in message:
            if message.get("event") == "error":
                log.warning("ws_error", code=message.get("code"), msg=message.get("msg"))
            return []
        arg = message.get("arg", {})
        channel = str(arg.get("channel", "")) if isinstance(arg, dict) else ""
        data = message.get("data", [])
        if not isinstance(data, list):
            return []
        out: list[ExchangeEvent] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if channel == "orders":
                ev, fill = parse_order_message(item, account_scope=self.account_scope, receive_ts=receive_ts)
                if ev is not None:
                    out.append(
                        ExchangeEvent(
                            kind="order",
                            receive_ts=receive_ts,
                            order_event=ev,
                            fill=fill,
                            raw={"source": "private_stream", "channel": channel, "item": item},
                        )
                    )
            elif channel == "positions":
                out.append(
                    ExchangeEvent(
                        kind="position",
                        receive_ts=receive_ts,
                        position=parse_position_message(item, receive_ts=receive_ts),
                        raw={"source": "private_stream", "channel": channel},
                    )
                )
            elif channel == "account":
                out.append(
                    ExchangeEvent(
                        kind="balance",
                        receive_ts=receive_ts,
                        balance=parse_account_message(item, receive_ts=receive_ts),
                        raw={"source": "private_stream", "channel": channel},
                    )
                )
            elif channel == "orders-algo":
                out.append(
                    ExchangeEvent(
                        kind="protection",
                        receive_ts=receive_ts,
                        protection=parse_algo_message(item, receive_ts=receive_ts),
                        raw={"source": "private_stream", "channel": channel},
                    )
                )
        return out

    async def run(self, sink: EventSink) -> None:
        """Boucle : lit, traduit, pousse au ``sink`` ; reconnecte de façon bornée sur coupure."""
        while True:
            try:
                for event in await self.read_once():
                    await sink(event)
            except asyncio.CancelledError:
                raise
            except ExchangeError:
                raise
            except Exception as exc:
                log.warning("ws_read_failed", error=str(exc))
                await sink(
                    ExchangeEvent(
                        kind="disconnect", receive_ts=self._clock.now_utc(), raw={"error": str(exc)}
                    )
                )
                await self.reconnect()

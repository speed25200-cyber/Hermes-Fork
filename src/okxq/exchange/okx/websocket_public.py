"""Protocole WebSocket public OKX v5 (https://app.okx.com/docs-v5/en/#overview-websocket, vérifié le 2026-09-18).

- souscription : ``{"op":"subscribe","args":[{"channel":"books","instId":"BTC-USDT-SWAP"}]}`` ;
  accusé : ``{"event":"subscribe","arg":{...},"connId":"..."}`` ; erreur : ``{"event":"error","code":"60012",
  "msg":"...","connId":"..."}`` ;
- désabonnement : ``{"op":"unsubscribe","args":[...]}`` → ``{"event":"unsubscribe",...}`` ;
- maintien : texte ``ping`` → texte ``pong`` (à envoyer si aucun message pendant 30 s) ;
- données : ``{"arg":{...},"action":"snapshot"|"update"?,"data":[...]}``.

Aucun canal privé n'est constructible ici : ``ensure_public`` refuse ``account``, ``positions``, ``orders``…
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from okxq.domain.errors import ConfigError, DataQualityError
from okxq.exchange.okx.mappings import PRIVATE_CHANNELS, is_business_channel

PING = "ping"
PONG = "pong"


@dataclass(frozen=True, slots=True)
class SubscriptionArg:
    channel: str
    inst_id: str | None = None
    inst_type: str | None = None

    def to_okx(self) -> dict[str, str]:
        arg = {"channel": self.channel}
        if self.inst_id is not None:
            arg["instId"] = self.inst_id
        if self.inst_type is not None:
            arg["instType"] = self.inst_type
        return arg

    @classmethod
    def from_okx(cls, raw: Any) -> SubscriptionArg | None:
        if not isinstance(raw, dict) or "channel" not in raw:
            return None
        return cls(channel=str(raw["channel"]), inst_id=raw.get("instId"), inst_type=raw.get("instType"))

    @property
    def endpoint(self) -> str:
        return "business" if is_business_channel(self.channel) else "public"


def ensure_public(args: Iterable[SubscriptionArg]) -> None:
    for arg in args:
        if arg.channel in PRIVATE_CHANNELS:
            raise ConfigError(f"canal privé interdit sur le collecteur public : {arg.channel}")


def build_subscribe(args: Sequence[SubscriptionArg]) -> str:
    ensure_public(args)
    if not args:
        raise ConfigError("souscription vide")
    return json.dumps({"op": "subscribe", "args": [a.to_okx() for a in args]}, separators=(",", ":"))


def build_unsubscribe(args: Sequence[SubscriptionArg]) -> str:
    ensure_public(args)
    if not args:
        raise ConfigError("désabonnement vide")
    return json.dumps({"op": "unsubscribe", "args": [a.to_okx() for a in args]}, separators=(",", ":"))


class WsMessageKind(StrEnum):
    PONG = "pong"
    EVENT = "event"  # subscribe / unsubscribe / channel-conn-count...
    ERROR = "error"
    DATA = "data"


@dataclass(frozen=True, slots=True)
class WsMessage:
    kind: WsMessageKind
    raw: dict[str, Any] | None = None
    event: str | None = None
    code: str | None = None
    msg: str | None = None
    arg: SubscriptionArg | None = None
    conn_id: str | None = None

    @property
    def is_data(self) -> bool:
        return self.kind is WsMessageKind.DATA


def parse_message(text: str) -> WsMessage:
    """Classe un message texte ; une trame non JSON (autre que ``pong``) est une erreur de contrat."""
    if text == PONG:
        return WsMessage(WsMessageKind.PONG)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataQualityError(f"trame WebSocket non JSON : {text[:80]!r}") from exc
    if not isinstance(raw, dict):
        raise DataQualityError("trame WebSocket non objet")
    event = raw.get("event")
    if event is not None:
        arg = SubscriptionArg.from_okx(raw.get("arg"))
        if event == "error":
            return WsMessage(
                WsMessageKind.ERROR,
                raw=raw,
                event="error",
                code=str(raw.get("code", "")),
                msg=str(raw.get("msg", "")),
                arg=arg,
                conn_id=raw.get("connId"),
            )
        return WsMessage(WsMessageKind.EVENT, raw=raw, event=str(event), arg=arg, conn_id=raw.get("connId"))
    if "arg" in raw and "data" in raw:
        arg = SubscriptionArg.from_okx(raw.get("arg"))
        if arg is None:
            raise DataQualityError("message de données sans arg.channel")
        return WsMessage(WsMessageKind.DATA, raw=raw, arg=arg)
    raise DataQualityError(f"trame WebSocket inconnue : {list(raw)[:5]}")

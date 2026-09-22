"""Operator alerts: Telegram when ``HERMES_TELEGRAM_TOKEN`` and ``HERMES_TELEGRAM_CHAT`` are set, logs always."""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("hermes.alerts")


async def alert(message: str, level: str = "INFO") -> None:
    getattr(log, level.lower(), log.info)(message)
    token = os.environ.get("HERMES_TELEGRAM_TOKEN")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT")
    if not token or not chat:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": f"[Hermes {level}] {message}"[:4000]},
            )
    except httpx.HTTPError as exc:  # alerts must never break trading
        log.warning("telegram alert failed: %s", exc)

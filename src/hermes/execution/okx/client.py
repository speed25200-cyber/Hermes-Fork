"""Asynchronous OKX v5 REST client.

Properties that matter for real money:

* **Signed requests** (HMAC-SHA256 of ``timestamp + METHOD + path + body``, base64) with a timestamp taken
  from a clock synchronised on ``/public/time`` -- local clock drift otherwise rejects every call (50102).
* **Rate limiting** with per-endpoint token buckets set below the documented limits.
* **Never a blind retry of an order.** Reads are retried with backoff; a POST that times out is resolved
  by *querying* the order through its client id (``clOrdId``), which the caller always sets.
* **Every response checked**: envelope ``code`` and per-item ``sCode``. An error is an exception, never a
  silently empty result.
* Demo trading via the ``x-simulated-trading: 1`` header.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

RETRYABLE_CODES = {"50001", "50004", "50011", "50013", "50026", "51149"}


class OKXError(RuntimeError):
    def __init__(self, code: str, msg: str, path: str, data: Any = None):
        super().__init__(f"OKX {path} error {code}: {msg}")
        self.code = code
        self.msg = msg
        self.path = path
        self.data = data


@dataclass(frozen=True)
class Credentials:
    api_key: str
    secret: str
    passphrase: str

    def __repr__(self) -> str:  # never print secrets
        return f"Credentials(api_key={self.api_key[:4]}…)"


class TokenBucket:
    def __init__(self, rate: float, per: float):
        self.capacity = rate
        self.tokens = rate
        self.per = per
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.capacity / self.per)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) * self.per / self.capacity)


# Requests per 2 seconds, kept ~20% under the documented limits.
LIMITS: dict[str, float] = {
    "/api/v5/public/time": 8,
    "/api/v5/public/instruments": 16,
    "/api/v5/market/ticker": 16,
    "/api/v5/market/tickers": 16,
    "/api/v5/market/books": 32,
    "/api/v5/market/candles": 32,
    "/api/v5/public/funding-rate": 8,
    "/api/v5/public/mark-price": 8,
    "/api/v5/account/balance": 8,
    "/api/v5/account/positions": 8,
    "/api/v5/account/config": 4,
    "/api/v5/account/set-position-mode": 4,
    "/api/v5/account/set-leverage": 16,
    "/api/v5/trade/order": 48,
    "/api/v5/trade/amend-order": 48,
    "/api/v5/trade/cancel-order": 48,
    "/api/v5/trade/orders-pending": 48,
    "/api/v5/trade/close-position": 16,
    "/api/v5/trade/order-algo": 16,
    "/api/v5/trade/cancel-algos": 16,
    "/api/v5/trade/orders-algo-pending": 16,
    "/api/v5/trade/fills": 48,
}


class OKXClient:
    def __init__(
        self,
        credentials: Credentials | None,
        base_url: str = "https://www.okx.com",
        demo: bool = True,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ):
        self.creds = credentials
        self.base_url = base_url.rstrip("/")
        self.demo = demo
        self.http = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self.clock_offset_ms = 0.0
        self.buckets = {k: TokenBucket(v, 2.0) for k, v in LIMITS.items()}
        self.default_bucket = TokenBucket(8, 2.0)

    async def close(self) -> None:
        await self.http.aclose()

    # -- signing --------------------------------------------------------------------------------------------
    def _timestamp(self) -> str:
        t = datetime.fromtimestamp((time.time() * 1000 + self.clock_offset_ms) / 1000, tz=UTC)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

    @staticmethod
    def sign(secret: str, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method.upper()}{path}{body}".encode()
        return base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()

    def _headers(self, method: str, path: str, body: str, auth: bool) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.demo:
            h["x-simulated-trading"] = "1"
        if auth:
            if self.creds is None:
                raise OKXError("NO_CREDENTIALS", "private endpoint without credentials", path)
            ts = self._timestamp()
            h.update(
                {
                    "OK-ACCESS-KEY": self.creds.api_key,
                    "OK-ACCESS-SIGN": self.sign(self.creds.secret, ts, method, path, body),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.creds.passphrase,
                }
            )
        return h

    async def sync_clock(self) -> float:
        t0 = time.time() * 1000
        data = await self.request("GET", "/api/v5/public/time")
        t1 = time.time() * 1000
        server = float(data[0]["ts"])
        self.clock_offset_ms = server - (t0 + t1) / 2
        return self.clock_offset_ms

    # -- transport ------------------------------------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | list[Any] | None = None,
        auth: bool = False,
        retries: int | None = None,
    ) -> list[dict[str, Any]]:
        method = method.upper()
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                query = "?" + urlencode(clean)
        full_path = path + query
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        # Reads are safe to retry; writes are not (the caller reconciles through clOrdId instead).
        attempts = retries if retries is not None else (4 if method == "GET" else 1)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self.buckets.get(path, self.default_bucket).acquire()
            try:
                r = await self.http.request(
                    method, full_path, content=payload or None, headers=self._headers(method, full_path, payload, auth)
                )
                try:
                    data = r.json()
                except ValueError as exc:
                    raise OKXError(str(r.status_code), r.text[:200], path) from exc
                code = str(data.get("code", ""))
                if code == "0":
                    return list(data.get("data") or [])
                items = data.get("data") or []
                if code in ("1", "2") and items:
                    first = items[0]
                    raise OKXError(str(first.get("sCode", code)), str(first.get("sMsg", data.get("msg"))), path, items)
                err = OKXError(code, str(data.get("msg")), path, items)
                if code == "50102" and attempt + 1 < attempts:
                    await self.sync_clock()
                    continue
                if code in RETRYABLE_CODES and attempt + 1 < attempts:
                    last_exc = err
                    await asyncio.sleep(min(0.5 * 2**attempt, 5))
                    continue
                raise err
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(0.5 * 2**attempt, 5))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    # -- public ---------------------------------------------------------------------------------------------
    async def instruments(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v5/public/instruments", {"instType": "SWAP"})

    async def tickers(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v5/market/tickers", {"instType": "SWAP"})

    async def books(self, inst_id: str, depth: int = 5) -> dict[str, Any]:
        d = await self.request("GET", "/api/v5/market/books", {"instId": inst_id, "sz": depth})
        return d[0] if d else {}

    async def funding_rate(self, inst_id: str) -> dict[str, Any]:
        d = await self.request("GET", "/api/v5/public/funding-rate", {"instId": inst_id})
        return d[0] if d else {}

    # -- account --------------------------------------------------------------------------------------------
    async def balance(self, ccy: str = "USDT") -> dict[str, Any]:
        d = await self.request("GET", "/api/v5/account/balance", {"ccy": ccy}, auth=True)
        return d[0] if d else {}

    async def positions(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v5/account/positions", {"instType": "SWAP"}, auth=True)

    async def account_config(self) -> dict[str, Any]:
        d = await self.request("GET", "/api/v5/account/config", auth=True)
        return d[0] if d else {}

    async def set_position_mode(self, mode: str = "net_mode") -> None:
        await self.request("POST", "/api/v5/account/set-position-mode", body={"posMode": mode}, auth=True)

    async def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "cross") -> None:
        await self.request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode},
            auth=True,
        )

    # -- trading --------------------------------------------------------------------------------------------
    async def place_order(self, **order: Any) -> dict[str, Any]:
        d = await self.request("POST", "/api/v5/trade/order", body=order, auth=True)
        return d[0]

    async def amend_order(
        self, inst_id: str, cl_ord_id: str, new_px: str | None = None, new_sz: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"instId": inst_id, "clOrdId": cl_ord_id, "cxlOnFail": False}
        if new_px is not None:
            body["newPx"] = new_px
        if new_sz is not None:
            body["newSz"] = new_sz
        d = await self.request("POST", "/api/v5/trade/amend-order", body=body, auth=True)
        return d[0]

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> dict[str, Any]:
        d = await self.request(
            "POST", "/api/v5/trade/cancel-order", body={"instId": inst_id, "clOrdId": cl_ord_id}, auth=True
        )
        return d[0]

    async def get_order(self, inst_id: str, cl_ord_id: str) -> dict[str, Any] | None:
        try:
            d = await self.request("GET", "/api/v5/trade/order", {"instId": inst_id, "clOrdId": cl_ord_id}, auth=True)
        except OKXError as exc:
            if exc.code in ("51603",):
                return None
            raise
        return d[0] if d else None

    async def pending_orders(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v5/trade/orders-pending", {"instType": "SWAP"}, auth=True)

    async def close_position(self, inst_id: str, mgn_mode: str = "cross", cl_ord_id: str | None = None) -> None:
        body = {"instId": inst_id, "mgnMode": mgn_mode, "autoCxl": True}
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        await self.request("POST", "/api/v5/trade/close-position", body=body, auth=True)

    async def cancel_all_after(self, timeout_s: int) -> dict[str, Any]:
        d = await self.request("POST", "/api/v5/trade/cancel-all-after", body={"timeOut": str(timeout_s)}, auth=True)
        return d[0] if d else {}

    async def place_algo(self, **algo: Any) -> dict[str, Any]:
        d = await self.request("POST", "/api/v5/trade/order-algo", body=algo, auth=True)
        return d[0]

    async def pending_algos(self, ord_type: str = "conditional") -> list[dict[str, Any]]:
        return await self.request(
            "GET", "/api/v5/trade/orders-algo-pending", {"instType": "SWAP", "ordType": ord_type}, auth=True
        )

    async def cancel_algos(self, items: list[dict[str, str]]) -> None:
        for i in range(0, len(items), 10):
            await self.request("POST", "/api/v5/trade/cancel-algos", body=items[i : i + 10], auth=True)

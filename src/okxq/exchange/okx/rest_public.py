"""Client REST public OKX v5 (§46) sur ``httpx.AsyncClient`` à transport injectable.

- endpoints : ``/api/v5/public/time``, ``/instruments``, ``/open-interest``, ``/funding-rate-history``,
  ``/mark-price``, ``/price-limit``, ``/market/books``, ``/market/history-candles``,
  ``/market/history-trades``, ``/market/tickers`` (référence https://app.okx.com/docs-v5/en/, vérifiée le
  2026-09-18 ; fixtures ``tests/fixtures/okx/rest_*.json``) ;
- enveloppe de réponse ``{"code":"0","msg":"","data":[...]}`` : tout ``code != "0"`` est une erreur
  métier (``ExchangeError`` avec le code OKX), un statut HTTP ≠ 200 aussi ;
- limitation de débit : un seau à jetons SIMPLE par portée documentée dans le manifeste de capacités
  (capacité = requêtes autorisées, recharge = requêtes / fenêtre) ; le seau attend, il ne rejette pas ;
- pagination des historiques (bougies, trades, funding) : OKX rend les enregistrements du plus récent au
  plus ancien ; ``after`` = strictement antérieurs à l'horodatage donné. ``paginate_history`` parcourt
  ``[start_ms, end_ms]`` par curseurs successifs, déduplique, détecte l'absence de progression, expose
  un point de reprise (``PageCheckpoint``) et, pour les séries à pas fixe, la liste des trous.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import httpx

from okxq.domain.errors import DataQualityError, ExchangeError
from okxq.exchange.okx.capabilities import CapabilityManifest, RegionProfile, load_manifest
from okxq.exchange.okx.mappings import BAR_MILLISECONDS, CANDLE_BARS

SIMULATED_TRADING_HEADER = "x-simulated-trading"


class TokenBucket:
    """Seau à jetons : ``capacity`` jetons, recharge continue de ``refill_per_second`` ; ``acquire`` attend.

    ``monotonic`` et ``sleep`` sont injectables pour des tests déterministes sans temps réel.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ExchangeError("paramètres de seau invalides", capacity=capacity, refill=refill_per_second)
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._tokens = self.capacity
        self._last = self._monotonic()
        self.waited_total_s = 0.0

    def _refill(self) -> None:
        now = self._monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill_per_second)
        self._last = now

    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self) -> float:
        """Consomme un jeton, en attendant le temps nécessaire ; retourne le temps attendu (s)."""
        self._refill()
        waited = 0.0
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self.refill_per_second
            await self._sleep(wait)
            waited = wait
            self.waited_total_s += wait
            self._refill()
            if self._tokens < 1.0:  # horloge injectée qui n'avance pas : on crédite le temps attendu
                self._tokens = 1.0
        self._tokens -= 1.0
        return waited


@dataclass(frozen=True, slots=True)
class PageCheckpoint:
    """Point de reprise d'une pagination : curseur ``after`` suivant et état atteint."""

    cursor_after: str | None
    pages: int
    rows: int
    oldest_ts_ms: int | None
    newest_ts_ms: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class PaginationResult:
    rows: list[dict[str, Any]]  # triés par horodatage CROISSANT, dédupliqués, bornés à [start, end]
    pages: int
    duplicates_removed: int
    gaps: list[tuple[int, int]]  # (ts précédent, ts suivant) quand l'écart ≠ pas attendu
    complete: bool  # la fenêtre demandée a été parcourue jusqu'à start_ms (ou jusqu'au début des données)
    checkpoint: PageCheckpoint
    start_ms: int
    end_ms: int

    @property
    def first_ts_ms(self) -> int | None:
        return int(self.rows[0]["_ts_ms"]) if self.rows else None

    @property
    def last_ts_ms(self) -> int | None:
        return int(self.rows[-1]["_ts_ms"]) if self.rows else None


async def paginate_history(
    fetch_page: Callable[[str | None], Awaitable[Sequence[Mapping[str, Any]]]],
    *,
    ts_of: Callable[[Mapping[str, Any]], int],
    key_of: Callable[[Mapping[str, Any]], Hashable],
    start_ms: int,
    end_ms: int,
    page_limit: int,
    expected_step_ms: int | None = None,
    max_pages: int = 1000,
    resume: PageCheckpoint | None = None,
) -> PaginationResult:
    """Parcourt ``[start_ms, end_ms]`` du plus récent au plus ancien par curseur ``after`` (exclusif)."""
    if start_ms > end_ms:
        raise DataQualityError("fenêtre de pagination inversée", start_ms=start_ms, end_ms=end_ms)
    if page_limit < 1:
        raise DataQualityError("page_limit doit être ≥ 1")
    after: str | None = resume.cursor_after if resume is not None else str(end_ms + 1)
    pages = 0
    duplicates = 0
    seen: set[Hashable] = set()
    kept: dict[Hashable, dict[str, Any]] = {}
    complete = False
    while pages < max_pages:
        page = await fetch_page(after)
        pages += 1
        if not page:
            complete = True
            break
        timestamps = [ts_of(r) for r in page]
        oldest = min(timestamps)
        if after is not None and oldest >= int(after):
            raise DataQualityError(
                "pagination sans progression : le curseur ne recule pas", cursor=after, oldest=oldest
            )
        for row, ts in zip(page, timestamps, strict=True):
            if ts > end_ms or ts < start_ms:
                continue
            key = key_of(row)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            enriched = dict(row)
            enriched["_ts_ms"] = ts
            kept[key] = enriched
        if oldest <= start_ms or len(page) < page_limit:
            complete = True
            break
        after = str(oldest)
    rows = sorted(kept.values(), key=lambda r: int(r["_ts_ms"]))
    gaps: list[tuple[int, int]] = []
    if expected_step_ms is not None:
        for prev, nxt in pairwise(rows):
            if int(nxt["_ts_ms"]) - int(prev["_ts_ms"]) != expected_step_ms:
                gaps.append((int(prev["_ts_ms"]), int(nxt["_ts_ms"])))
    checkpoint = PageCheckpoint(
        cursor_after=None if complete else after,
        pages=pages,
        rows=len(rows),
        oldest_ts_ms=int(rows[0]["_ts_ms"]) if rows else None,
        newest_ts_ms=int(rows[-1]["_ts_ms"]) if rows else None,
        complete=complete,
    )
    return PaginationResult(rows, pages, duplicates, gaps, complete, checkpoint, start_ms, end_ms)


class OkxPublicRestClient:
    """Client des endpoints publics. Aucun secret : aucune signature, aucun endpoint privé."""

    def __init__(
        self,
        region: RegionProfile,
        *,
        manifest: CapabilityManifest | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.region = region
        self.manifest = manifest or load_manifest()
        headers = {"accept": "application/json", "user-agent": "okxq/0.1"}
        if region.simulated_trading:
            headers[SIMULATED_TRADING_HEADER] = "1"
        self._client = httpx.AsyncClient(
            base_url=region.rest_base_url, transport=transport, timeout=timeout_s, headers=headers
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self.requests_made = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OkxPublicRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def bucket_for(self, path: str) -> TokenBucket:
        op = self.manifest.rest_operation(path)
        scope = f"{op.rate_limit.scope}:{path}"
        bucket = self._buckets.get(scope)
        if bucket is None:
            bucket = TokenBucket(
                op.rate_limit.requests,
                op.rate_limit.requests / op.rate_limit.per_seconds,
                monotonic=self._monotonic,
                sleep=self._sleep,
            )
            self._buckets[scope] = bucket
        return bucket

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> list[Any]:
        """GET limité en débit ; retourne ``data`` ou lève ``ExchangeError`` (HTTP ≠ 200 ou ``code != "0"``)."""
        await self.bucket_for(path).acquire()
        try:
            response = await self._client.get(path, params=dict(params or {}))
        except httpx.TimeoutException as exc:
            raise ExchangeError("délai dépassé sur endpoint public", code="TIMEOUT", path=path) from exc
        except httpx.HTTPError as exc:
            raise ExchangeError(f"erreur réseau : {exc}", code="NETWORK", path=path) from exc
        self.requests_made += 1
        if response.status_code != 200:
            raise ExchangeError(
                f"HTTP {response.status_code} sur {path}", code=f"HTTP_{response.status_code}", path=path
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ExchangeError("réponse non JSON", code="BAD_JSON", path=path) from exc
        if not isinstance(body, dict) or "code" not in body:
            raise ExchangeError("enveloppe de réponse inattendue", code="BAD_ENVELOPE", path=path)
        if str(body["code"]) != "0":
            raise ExchangeError(
                f"code métier OKX {body['code']} : {body.get('msg', '')}", code=str(body["code"]), path=path
            )
        data = body.get("data")
        if not isinstance(data, list):
            raise ExchangeError("champ data absent ou non liste", code="BAD_DATA", path=path)
        return data

    # --- endpoints simples ---------------------------------------------------------------------------------

    async def server_time(self) -> int:
        data = await self.get("/api/v5/public/time")
        if not data or "ts" not in data[0]:
            raise ExchangeError("réponse time sans ts", code="BAD_DATA")
        return int(str(data[0]["ts"]))

    async def instruments(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict[str, Any]]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return [dict(x) for x in await self.get("/api/v5/public/instruments", params)]

    async def open_interest(
        self, inst_type: str = "SWAP", inst_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return [dict(x) for x in await self.get("/api/v5/public/open-interest", params)]

    async def mark_price(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict[str, Any]]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return [dict(x) for x in await self.get("/api/v5/public/mark-price", params)]

    async def price_limit(self, inst_id: str) -> dict[str, Any]:
        data = await self.get("/api/v5/public/price-limit", {"instId": inst_id})
        if not data:
            raise ExchangeError("price-limit vide", code="BAD_DATA", inst_id=inst_id)
        return dict(data[0])

    async def books(self, inst_id: str, sz: int = 400) -> dict[str, Any]:
        """Instantané REST : bootstrap/diagnostic seulement, jamais fusionné avec le flux WS (T13)."""
        data = await self.get("/api/v5/market/books", {"instId": inst_id, "sz": str(sz)})
        if not data:
            raise ExchangeError("carnet REST vide", code="BAD_DATA", inst_id=inst_id)
        book = dict(data[0])
        book.setdefault("instId", inst_id)
        return book

    async def tickers(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return [dict(x) for x in await self.get("/api/v5/market/tickers", {"instType": inst_type})]

    # --- historiques paginés ----------------------------------------------------------------------------

    async def history_candles(
        self,
        inst_id: str,
        *,
        bar: str = "1m",
        start_ms: int,
        end_ms: int,
        limit: int = 100,
        max_pages: int = 1000,
        resume: PageCheckpoint | None = None,
    ) -> PaginationResult:
        if bar not in CANDLE_BARS:
            raise DataQualityError(f"barre non supportée : {bar!r}")
        step = BAR_MILLISECONDS[CANDLE_BARS[bar]]

        async def fetch(after: str | None) -> Sequence[Mapping[str, Any]]:
            params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
            if after is not None:
                params["after"] = after
            rows = await self.get("/api/v5/market/history-candles", params)
            return [{"row": list(r)} for r in rows]

        return await paginate_history(
            fetch,
            ts_of=lambda r: int(str(r["row"][0])),
            key_of=lambda r: int(str(r["row"][0])),
            start_ms=start_ms,
            end_ms=end_ms,
            page_limit=limit,
            expected_step_ms=step,
            max_pages=max_pages,
            resume=resume,
        )

    async def history_trades(
        self,
        inst_id: str,
        *,
        start_ms: int,
        end_ms: int,
        limit: int = 100,
        max_pages: int = 1000,
        resume: PageCheckpoint | None = None,
    ) -> PaginationResult:
        async def fetch(after: str | None) -> Sequence[Mapping[str, Any]]:
            params = {"instId": inst_id, "type": "2", "limit": str(limit)}
            if after is not None:
                params["after"] = after
            return [dict(r) for r in await self.get("/api/v5/market/history-trades", params)]

        return await paginate_history(
            fetch,
            ts_of=lambda r: int(str(r["ts"])),
            key_of=lambda r: (str(r["instId"]), str(r["tradeId"])),
            start_ms=start_ms,
            end_ms=end_ms,
            page_limit=limit,
            max_pages=max_pages,
            resume=resume,
        )

    async def funding_rate_history(
        self,
        inst_id: str,
        *,
        start_ms: int,
        end_ms: int,
        limit: int = 100,
        max_pages: int = 1000,
        resume: PageCheckpoint | None = None,
    ) -> PaginationResult:
        async def fetch(after: str | None) -> Sequence[Mapping[str, Any]]:
            params = {"instId": inst_id, "limit": str(limit)}
            if after is not None:
                params["after"] = after
            return [dict(r) for r in await self.get("/api/v5/public/funding-rate-history", params)]

        return await paginate_history(
            fetch,
            ts_of=lambda r: int(str(r["fundingTime"])),
            key_of=lambda r: (str(r["instId"]), int(str(r["fundingTime"]))),
            start_ms=start_ms,
            end_ms=end_ms,
            page_limit=limit,
            max_pages=max_pages,
            resume=resume,
        )

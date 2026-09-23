"""Live feed: positioning snapshots land on the bar grid exactly as the research archives put them."""

import asyncio
import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd

from hermes.data.binance_archive import BinanceArchive
from hermes.data.live_feed import BinanceLiveFeed

STEP = 300_000  # 5-minute snapshots
CALLS: list[tuple[int, int]] = []


def _ratio(ts_ms: int) -> float:
    return 0.5 + (ts_ms // STEP) % 97 / 50.0


def _live_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.params
    now = int(datetime.now(UTC).timestamp() * 1000)
    if request.url.path.startswith("/futures/data/"):
        # Binance's semantics: a range holding more than `limit` snapshots returns the LATEST `limit` of it, and
        # a snapshot is stamped 5 minutes after the archives' create_time for the same value.
        start, end = int(p["startTime"]), int(p.get("endTime", now))
        if start < now - 30 * 86_400_000:
            return httpx.Response(400, json={"code": -1130, "msg": "startTime too old"})
        ts = list(range(-(-start // STEP) * STEP, min(end, now) + 1, STEP))[-int(p["limit"]) :]
        rows = [{"timestamp": t, "longShortRatio": str(_ratio(t - STEP))} for t in ts]
        CALLS.append((start, end))
        return httpx.Response(200, json=rows)
    if request.url.path in ("/fapi/v1/klines", "/fapi/v1/premiumIndexKlines"):
        step = 1_800_000
        start = -(-int(p["startTime"]) // step) * step
        rows = [
            [t, "1", "1.1", "0.9", "1", "10", t + step - 1, "10", 5, "4", "4", "0"]
            for t in range(start, now, step)[: int(p["limit"])]
        ]
        return httpx.Response(200, json=rows)
    if request.url.path == "/fapi/v1/fundingRate":
        return httpx.Response(200, json=[])
    return httpx.Response(404)


def _metrics_zip(day) -> bytes:
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    lines = [
        "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
        "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio"
    ]
    for t in range(t0, t0 + 86_400_000, STEP):
        stamp = datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{stamp},BTCUSDT,1,1,1,{_ratio(t)},1,1")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m.csv", "\n".join(lines))
    return buf.getvalue()


def test_live_positioning_matches_the_archives(tmp_path):
    day = datetime.now(UTC).date() - timedelta(days=2)
    arch = BinanceArchive(
        tmp_path,
        workers=2,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (
                    httpx.Response(200, content=_metrics_zip(day))
                    if r.url.path.endswith(f"BTCUSDT-metrics-{day.isoformat()}.zip")
                    else httpx.Response(404)
                )
            )
        ),
    )
    research = arch.metrics("BTCUSDT", "30m", day, day)["ls_top"]

    async def run():
        http = httpx.AsyncClient(base_url="https://fapi", transport=httpx.MockTransport(_live_handler))
        feed = BinanceLiveFeed("30m", history_bars=48 * 4, client=http, positioning=("ls_top",))
        panel = await feed.update(["BTCUSDT"])
        await feed.close()
        return feed, panel

    feed, panel = asyncio.run(run())
    live = panel["ls_top"]["BTCUSDT"]
    # Bars whose (open, close] lies within the one archived day (the last one also needs the next day's file).
    t = pd.Timestamp(day, tz="UTC")
    common = research.index.intersection(live.index)
    common = common[common < t + pd.Timedelta(hours=23, minutes=30)]
    assert len(common) >= 47
    np.testing.assert_allclose(live.reindex(common).to_numpy(), research.reindex(common).to_numpy())
    # The bar opening at 00:00 holds the snapshot of its close (00:30), known when the bar closes.
    assert np.isclose(live[t], _ratio(int((t + pd.Timedelta(minutes=30)).value // 1_000_000)))
    held = feed.pos["BTCUSDT"]
    assert held.index[0] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    # The whole 29.5 days are read (bounded windows), not just Binance's latest 500 snapshots.
    assert held.index[0] <= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=29)
    assert len(CALLS) >= 17 and all(e - s <= 499 * STEP for s, e in CALLS)

"""Générateur SYNTHÉTIQUE seedé d'événements de marché au format ``docs/event_schemas.md``.

Objet : exercer les pipelines (features, labels, splits, entraînement) hors ligne et de façon
reproductible. Le processus de prix est une marche aléatoire log-normale à facteur commun SANS alpha
planté par défaut : tout rapport calculé sur ces données porte la mention « données synthétiques — aucune
preuve d'alpha ». ``planted_momentum`` permet, dans un TEST, d'injecter un signal connu pour vérifier que
le pipeline sait le retrouver (contrôle positif), jamais pour produire un résultat présentable.

``available_at = exchange_ts + latency`` : la latence est SUPPOSÉE (drapeau ``LATENCY_ASSUMED``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from okxq.domain.clocks import ensure_utc
from okxq.domain.events import EventEnvelope
from okxq.domain.ids import payload_hash, sha256_hex
from okxq.features.market_events import sort_key

DEFAULT_INSTRUMENTS: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP")
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SyntheticSpec:
    seed: int = 25200
    instruments: tuple[str, ...] = DEFAULT_INSTRUMENTS
    start: datetime = datetime(2026, 6, 1, tzinfo=UTC)
    minutes: int = 240
    substeps_per_minute: int = 2  # mises à jour carnet/mark/index par minute
    latency_ms: int = 150
    sigma_factor_per_min: float = 0.0006
    sigma_idio_per_min: float = 0.0004
    trades_per_substep: float = 1.5
    funding_period_h: int = 8
    oi_interval_s: int = 300
    planted_momentum: float = 0.0  # 0 = aucun signal ; > 0 : rendement suivant = k · rendement 5 min (test)
    initial_prices: dict[str, float] = field(
        default_factory=lambda: {
            "BTC-USDT-SWAP": 60000.0,
            "ETH-USDT-SWAP": 3000.0,
            "SOL-USDT-SWAP": 150.0,
            "XRP-USDT-SWAP": 0.6,
        }
    )
    betas: dict[str, float] = field(
        default_factory=lambda: {
            "BTC-USDT-SWAP": 1.0,
            "ETH-USDT-SWAP": 1.1,
            "SOL-USDT-SWAP": 1.3,
            "XRP-USDT-SWAP": 0.9,
        }
    )


def _fmt(x: float, places: int) -> str:
    return f"{x:.{places}f}"


class _Emitter:
    def __init__(self, spec: SyntheticSpec) -> None:
        self.spec = spec
        self.seq = 0
        self.events: list[EventEnvelope] = []

    def emit(
        self, event_type: str, exchange_ts: datetime, payload: dict[str, Any], source: str = "synthetic"
    ) -> None:
        self.seq += 1
        receive = exchange_ts + timedelta(milliseconds=self.spec.latency_ms)
        self.events.append(
            EventEnvelope(
                event_id=f"syn_{self.seq:010d}",
                event_type=event_type,
                schema_version=SCHEMA_VERSION,
                source=source,
                exchange_ts=exchange_ts,
                receive_ts=receive,
                available_at=receive,
                ingest_seq=self.seq,
                payload_hash=payload_hash(payload),
                payload=payload,
            )
        )


def _price_places(p0: float) -> int:
    return 1 if p0 >= 1000 else (2 if p0 >= 10 else 4)


def generate_synthetic_events(spec: SyntheticSpec = SyntheticSpec()) -> list[EventEnvelope]:
    rng = np.random.default_rng(spec.seed)
    em = _Emitter(spec)
    start = ensure_utc(spec.start)
    n_inst = len(spec.instruments)
    log_px = {i: math.log(spec.initial_prices.get(i, 100.0)) for i in spec.instruments}
    places = {i: _price_places(spec.initial_prices.get(i, 100.0)) for i in spec.instruments}
    tick = {i: 10.0 ** (-places[i]) for i in spec.instruments}
    ct_val = {"BTC-USDT-SWAP": "0.01", "ETH-USDT-SWAP": "0.1"}
    oi = {i: 100000.0 for i in spec.instruments}
    for inst in spec.instruments:
        em.emit(
            "instrument",
            start,
            {
                "inst_id": inst,
                "inst_type": "SWAP",
                "ct_val": ct_val.get(inst, "1"),
                "ct_val_ccy": inst.split("-")[0],
                "ct_type": "linear",
                "ct_mult": "1",
                "settle_ccy": "USDT",
                "tick_sz": _fmt(tick[inst], places[inst]),
                "lot_sz": "1",
                "min_sz": "1",
                "state": "live",
                "lever": "50",
                "list_time_ms": str(int((start - timedelta(days=400)).timestamp() * 1000)),
            },
        )
    sub_s = 60.0 / spec.substeps_per_minute
    history: dict[str, list[float]] = {i: [] for i in spec.instruments}
    funding_period_s = spec.funding_period_h * 3600
    next_oi_ts = start
    # dernier règlement PASSÉ connu au démarrage (settled:true, funding_time <= start)
    start_epoch = int(start.timestamp())
    last_settle = start_epoch - (start_epoch % funding_period_s)
    for inst in spec.instruments:
        rate0 = 0.0001 + 0.00005 * float(rng.normal())
        em.emit(
            "funding",
            start,
            {
                "inst_id": inst,
                "funding_rate": _fmt(rate0, 8),
                "next_funding_rate": _fmt(rate0, 8),
                "funding_time_ms": str(last_settle * 1000),
                "next_funding_time_ms": str((last_settle + funding_period_s) * 1000),
                "settled": True,
                "realized_rate": _fmt(rate0, 8),
            },
        )
    for m in range(spec.minutes):
        minute_start = start + timedelta(minutes=m)
        f = rng.normal(0.0, spec.sigma_factor_per_min)
        idio = rng.normal(0.0, spec.sigma_idio_per_min, size=n_inst)
        ohlc: dict[str, list[float]] = {}
        for k, inst in enumerate(spec.instruments):
            r = spec.betas.get(inst, 1.0) * f + idio[k]
            past = history[inst]
            if spec.planted_momentum and len(past) >= 5:
                r += spec.planted_momentum * (past[-1] - past[-6])
            o = math.exp(log_px[inst])
            path = [o]
            for _ in range(spec.substeps_per_minute):
                log_px[inst] += r / spec.substeps_per_minute
                path.append(math.exp(log_px[inst]))
            history[inst].append(log_px[inst])
            ohlc[inst] = path
        for s in range(spec.substeps_per_minute):
            ts = minute_start + timedelta(seconds=(s + 1) * sub_s - 0.5)
            for inst in spec.instruments:
                px = ohlc[inst][s + 1]
                rel_half_spread = 0.00005 + 0.00005 * float(rng.random())
                half = max(tick[inst] / 2.0, px * rel_half_spread)
                bid1 = math.floor((px - half) / tick[inst]) * tick[inst]
                ask1 = math.ceil((px + half) / tick[inst]) * tick[inst]
                if ask1 <= bid1:
                    ask1 = bid1 + tick[inst]
                depth = 50.0 + 50.0 * float(rng.random())
                bids = [
                    [
                        _fmt(bid1 - j * tick[inst], places[inst]),
                        _fmt(depth * (1 + j) * (0.8 + 0.4 * float(rng.random())), 0),
                    ]
                    for j in range(5)
                ]
                asks = [
                    [
                        _fmt(ask1 + j * tick[inst], places[inst]),
                        _fmt(depth * (1 + j) * (0.8 + 0.4 * float(rng.random())), 0),
                    ]
                    for j in range(5)
                ]
                em.emit(
                    "book.snapshot",
                    ts,
                    {
                        "inst_id": inst,
                        "channel": "books",
                        "seq_id": str(em.seq + 1),
                        "prev_seq_id": str(em.seq),
                        "checksum": "0",
                        "ts_ms": str(int(ts.timestamp() * 1000)),
                        "bids": bids,
                        "asks": asks,
                    },
                )
                em.emit(
                    "mark_price",
                    ts,
                    {
                        "inst_id": inst,
                        "mark_px": _fmt(px * (1 + 1e-5 * float(rng.normal())), places[inst] + 1),
                        "ts_ms": str(int(ts.timestamp() * 1000)),
                    },
                )
                em.emit(
                    "index_price",
                    ts,
                    {
                        "inst_id": inst,
                        "idx_px": _fmt(px * (1 - 2e-5), places[inst] + 1),
                        "ts_ms": str(int(ts.timestamp() * 1000)),
                    },
                )
                n_tr = int(rng.poisson(spec.trades_per_substep))
                for _j in range(n_tr):
                    tts = ts - timedelta(seconds=float(rng.random()) * sub_s * 0.9)
                    side = "buy" if rng.random() < 0.5 + 0.2 * math.tanh(r * 2000) else "sell"
                    price = ask1 if side == "buy" else bid1
                    em.emit(
                        "trade",
                        tts,
                        {
                            "inst_id": inst,
                            "trade_id": f"t{em.seq + 1}",
                            "price": _fmt(price, places[inst]),
                            "qty_contracts": str(int(1 + rng.integers(1, 20))),
                            "side": side,
                            "ts_ms": str(int(tts.timestamp() * 1000)),
                        },
                    )
        close_ts = minute_start + timedelta(seconds=60)
        for inst in spec.instruments:
            path = ohlc[inst]
            vol = float(10 + rng.integers(0, 200))
            payload = {
                "inst_id": inst,
                "ts_ms": str(int(minute_start.timestamp() * 1000)),
                "open": _fmt(path[0], places[inst] + 1),
                "high": _fmt(max(path) * (1 + 1e-5), places[inst] + 1),
                "low": _fmt(min(path) * (1 - 1e-5), places[inst] + 1),
                "close": _fmt(path[-1], places[inst] + 1),
                "vol_contracts": _fmt(vol, 0),
                "vol_base": _fmt(vol * float(ct_val.get(inst, "1")), 4),
                "vol_quote": _fmt(vol * float(ct_val.get(inst, "1")) * path[-1], 2),
                "confirm": "1",
            }
            em.emit("candle.1m", close_ts, payload)
            intrabar = dict(payload)
            intrabar["confirm"] = "0"
            intrabar["ts_ms"] = str(int(close_ts.timestamp() * 1000))
            intrabar["close"] = intrabar["open"] = _fmt(path[-1], places[inst] + 1)
            em.emit("candle.1m", close_ts + timedelta(seconds=20), intrabar)
        if close_ts >= next_oi_ts:
            for inst in spec.instruments:
                oi[inst] *= 1 + 0.002 * float(rng.normal())
                em.emit(
                    "open_interest",
                    close_ts,
                    {
                        "inst_id": inst,
                        "oi_contracts": _fmt(oi[inst], 0),
                        "oi_base": _fmt(oi[inst] * float(ct_val.get(inst, "1")), 4),
                        "ts_ms": str(int(close_ts.timestamp() * 1000)),
                    },
                )
            next_oi_ts = close_ts + timedelta(seconds=spec.oi_interval_s)
        epoch = int(close_ts.timestamp())
        if epoch % 3600 == 0:
            period_start = epoch - (epoch % funding_period_s)
            next_settle = period_start + funding_period_s
            for inst in spec.instruments:
                rate = 0.0001 + 0.00005 * float(rng.normal())
                if epoch == period_start:
                    em.emit(
                        "funding",
                        close_ts,
                        {
                            "inst_id": inst,
                            "funding_rate": _fmt(rate, 8),
                            "next_funding_rate": _fmt(rate, 8),
                            "funding_time_ms": str(period_start * 1000),
                            "next_funding_time_ms": str(next_settle * 1000),
                            "settled": True,
                            "realized_rate": _fmt(rate, 8),
                        },
                    )
                em.emit(
                    "funding",
                    close_ts + timedelta(seconds=1),
                    {
                        "inst_id": inst,
                        "funding_rate": _fmt(rate, 8),
                        "next_funding_rate": _fmt(rate, 8),
                        "funding_time_ms": str(next_settle * 1000),
                        "next_funding_time_ms": str((next_settle + funding_period_s) * 1000),
                        "settled": False,
                        "realized_rate": None,
                    },
                )
    return sorted(em.events, key=sort_key)


def write_event_folder(
    events: Sequence[EventEnvelope],
    folder: Path,
    *,
    dataset: str,
    quality_level: str,
    notes: str,
    latency_assumed: bool,
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    ordered = sorted(events, key=sort_key)
    lines = [json.dumps(e.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) for e in ordered]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    (folder / "events.jsonl").write_bytes(body)
    manifest = {
        "dataset": dataset,
        "schema_version": SCHEMA_VERSION,
        "instruments": sorted({str(e.payload.get("inst_id")) for e in ordered if e.payload.get("inst_id")}),
        "first_available_at": ordered[0].available_at.isoformat() if ordered else None,
        "last_available_at": ordered[-1].available_at.isoformat() if ordered else None,
        "rows": len(ordered),
        "sha256": sha256_hex(body),
        "quality_level": quality_level,
        "notes": notes,
        "latency_assumed": latency_assumed,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def read_event_folder(
    folder: Path, *, verify_sha256: bool = True
) -> tuple[list[EventEnvelope], dict[str, Any]]:
    path = folder / "events.jsonl"
    body = path.read_bytes()
    manifest_path = folder / "manifest.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    if verify_sha256 and manifest.get("sha256") and manifest["sha256"] != sha256_hex(body):
        raise ValueError(f"events.jsonl ne correspond pas au sha256 du manifeste ({folder})")
    events = [
        EventEnvelope.model_validate_json(line) for line in body.decode("utf-8").splitlines() if line.strip()
    ]
    return sorted(events, key=sort_key), manifest

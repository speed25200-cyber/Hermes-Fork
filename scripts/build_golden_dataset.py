"""Construit le jeu golden ``tests/fixtures/golden`` (``manifest.json`` + ``events.jsonl``) de façon DÉTERMINISTE.

Le jeu est SYNTHÉTIQUE (``quality_level: synthetic``) : il reproduit les formes documentées OKX (fixtures
``tests/fixtures/okx``) avec un chemin de prix pseudo-aléatoire à graine fixe, des carnets à séquences
continues (non consécutives), un message de maintien, des bougies cohérentes avec les trades, un funding
estimé puis réglé. Il sert au replay, au smoke test hors ligne et à ``okxq data inspect`` — jamais à
prétendre à des statistiques de marché réelles.

Usage : ``.venv/bin/python scripts/build_golden_dataset.py [--out tests/fixtures/golden]``.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from okxq.data.archive import write_jsonl_dataset
from okxq.data.normalizer import Normalizer

from okxq.domain.clocks import SimulatedClock
from okxq.domain.events import EventEnvelope

ROOT = Path(__file__).resolve().parents[1]
T0_MS = 1_789_732_800_000  # 2026-09-18T12:00:00Z
DURATION_S = 240
LATENCY_MS = 30
SEED = 25200
LEVELS = 5

INSTRUMENTS: dict[str, dict[str, Any]] = {
    "BTC-USDT-SWAP": {"mid": Decimal("65000.0"), "tick": Decimal("0.1"), "ct_val": Decimal("0.01"), "lot": 1},
    "ETH-USDT-SWAP": {"mid": Decimal("3200.00"), "tick": Decimal("0.01"), "ct_val": Decimal("0.1"), "lot": 1},
}


def _ms_dt(ms: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)


class _BookSim:
    def __init__(self, inst_id: str, spec: dict[str, Any], rng: random.Random) -> None:
        self.inst_id = inst_id
        self.mid: Decimal = spec["mid"]
        self.tick: Decimal = spec["tick"]
        self.rng = rng
        self.seq = 100_000
        self.levels: dict[str, dict[Decimal, Decimal]] = {"bids": {}, "asks": {}}

    def _target(self) -> dict[str, dict[Decimal, Decimal]]:
        bids = {self.mid - self.tick * (k + 1): Decimal(self.rng.randint(1, 50)) for k in range(LEVELS)}
        asks = {self.mid + self.tick * (k + 1): Decimal(self.rng.randint(1, 50)) for k in range(LEVELS)}
        return {"bids": bids, "asks": asks}

    @staticmethod
    def _fmt(levels: dict[Decimal, Decimal]) -> list[list[str]]:
        return [[format(p, "f"), format(q, "f"), "0", "1"] for p, q in levels.items()]

    def snapshot(self, ts_ms: int) -> dict[str, Any]:
        self.levels = self._target()
        return {
            "arg": {"channel": "books", "instId": self.inst_id},
            "action": "snapshot",
            "data": [
                {
                    "asks": self._fmt(self.levels["asks"]),
                    "bids": self._fmt(self.levels["bids"]),
                    "ts": str(ts_ms),
                    "checksum": 0,
                    "prevSeqId": -1,
                    "seqId": self.seq,
                }
            ],
        }

    def update(self, ts_ms: int) -> dict[str, Any]:
        step = self.rng.choice([-2, -1, 0, 0, 1, 2])
        self.mid += self.tick * step
        target = self._target()
        diff: dict[str, dict[Decimal, Decimal]] = {"bids": {}, "asks": {}}
        for side in ("bids", "asks"):
            for price in self.levels[side]:
                if price not in target[side]:
                    diff[side][price] = Decimal(0)
            for price, qty in target[side].items():
                if self.levels[side].get(price) != qty:
                    diff[side][price] = qty
        prev = self.seq
        self.seq += self.rng.choice([1, 1, 2, 3])  # seqId non nécessairement consécutifs
        self.levels = target
        return {
            "arg": {"channel": "books", "instId": self.inst_id},
            "action": "update",
            "data": [
                {
                    "asks": self._fmt(diff["asks"]),
                    "bids": self._fmt(diff["bids"]),
                    "ts": str(ts_ms),
                    "checksum": 0,
                    "prevSeqId": prev,
                    "seqId": self.seq,
                }
            ],
        }

    def heartbeat(self, ts_ms: int) -> dict[str, Any]:
        return {
            "arg": {"channel": "books", "instId": self.inst_id},
            "action": "update",
            "data": [
                {
                    "asks": [],
                    "bids": [],
                    "ts": str(ts_ms),
                    "checksum": 0,
                    "prevSeqId": self.seq,
                    "seqId": self.seq,
                }
            ],
        }


def build(out_dir: Path) -> dict[str, Any]:
    rng = random.Random(SEED)
    clock = SimulatedClock(_ms_dt(T0_MS))
    norm = Normalizer(clock, source="fixture-golden")
    events: list[EventEnvelope] = []

    def push(raw: dict[str, Any], ts_ms: int) -> None:
        clock.set(_ms_dt(ts_ms + LATENCY_MS))
        events.extend(norm.normalize_ws(raw))

    instruments = json.loads((ROOT / "tests" / "fixtures" / "okx" / "rest_instruments.json").read_text())
    linear = [i for i in instruments["response"]["data"] if i["instId"] in INSTRUMENTS]
    clock.set(_ms_dt(T0_MS))
    events.extend(norm.normalize_rest("public/instruments", linear))

    books = {inst: _BookSim(inst, spec, rng) for inst, spec in INSTRUMENTS.items()}
    trade_id = 130_000_000
    minute_trades: dict[str, list[tuple[Decimal, Decimal]]] = {inst: [] for inst in INSTRUMENTS}
    vol_ccy_24h = {inst: Decimal(20_000) for inst in INSTRUMENTS}
    funding_rate = Decimal("0.0001")
    for sim in books.values():
        push(sim.snapshot(T0_MS), T0_MS)
    for second in range(1, DURATION_S + 1):
        ts = T0_MS + second * 1000
        for inst, sim in books.items():
            spec = INSTRUMENTS[inst]
            if second % 2 == 0:
                push(sim.update(ts), ts)
            if second % 2 == 1:
                trade_id += 1
                side = rng.choice(["buy", "sell"])
                px = sim.mid + (sim.tick if side == "buy" else -sim.tick)
                sz = Decimal(rng.randint(1, 20))
                minute_trades[inst].append((px, sz))
                vol_ccy_24h[inst] += sz * spec["ct_val"]
                push(
                    {
                        "arg": {"channel": "trades", "instId": inst},
                        "data": [
                            {
                                "instId": inst,
                                "tradeId": str(trade_id),
                                "px": format(px, "f"),
                                "sz": format(sz, "f"),
                                "side": side,
                                "ts": str(ts + 123),
                                "count": "1",
                            }
                        ],
                    },
                    ts + 123,
                )
            if second % 5 == 0:
                push(
                    {
                        "arg": {"channel": "mark-price", "instId": inst},
                        "data": [
                            {
                                "instType": "SWAP",
                                "instId": inst,
                                "markPx": format(sim.mid, "f"),
                                "ts": str(ts + 200),
                            }
                        ],
                    },
                    ts + 200,
                )
                index_id = inst.removesuffix("-SWAP")
                push(
                    {
                        "arg": {"channel": "index-tickers", "instId": index_id},
                        "data": [
                            {
                                "instId": index_id,
                                "idxPx": format(sim.mid - sim.tick / 2, "f"),
                                "ts": str(ts + 300),
                            }
                        ],
                    },
                    ts + 300,
                )
            if second % 10 == 0:
                last = minute_trades[inst][-1][0] if minute_trades[inst] else sim.mid
                push(
                    {
                        "arg": {"channel": "tickers", "instId": inst},
                        "data": [
                            {
                                "instType": "SWAP",
                                "instId": inst,
                                "last": format(last, "f"),
                                "lastSz": "1",
                                "askPx": format(sim.mid + sim.tick, "f"),
                                "askSz": "10",
                                "bidPx": format(sim.mid - sim.tick, "f"),
                                "bidSz": "10",
                                "open24h": format(spec["mid"], "f"),
                                "high24h": format(spec["mid"] * Decimal("1.01"), "f"),
                                "low24h": format(spec["mid"] * Decimal("0.99"), "f"),
                                "volCcy24h": format(vol_ccy_24h[inst], "f"),
                                "vol24h": format(vol_ccy_24h[inst] / spec["ct_val"], "f"),
                                "ts": str(ts + 400),
                                "sodUtc0": format(spec["mid"], "f"),
                                "sodUtc8": format(spec["mid"], "f"),
                            }
                        ],
                    },
                    ts + 400,
                )
            if second % 30 == 0:
                oi = Decimal(1_000_000 + rng.randint(-5000, 5000))
                push(
                    {
                        "arg": {"channel": "open-interest", "instId": inst},
                        "data": [
                            {
                                "instType": "SWAP",
                                "instId": inst,
                                "oi": format(oi, "f"),
                                "oiCcy": format(oi * spec["ct_val"], "f"),
                                "oiUsd": format(oi * spec["ct_val"] * sim.mid, "f"),
                                "ts": str(ts + 500),
                            }
                        ],
                    },
                    ts + 500,
                )
            if second % 60 == 30 or second % 60 == 0:
                trades = minute_trades[inst]
                open_ts = T0_MS + ((second - 1) // 60) * 60_000
                if trades:
                    o, c = trades[0][0], trades[-1][0]
                    h, lo = max(p for p, _ in trades), min(p for p, _ in trades)
                    vol = sum((s for _, s in trades), Decimal(0))
                    vol_quote = sum((p * s * spec["ct_val"] for p, s in trades), Decimal(0))
                else:
                    o = c = h = lo = sim.mid
                    vol = vol_quote = Decimal(0)
                confirm = "1" if second % 60 == 0 else "0"
                push(
                    {
                        "arg": {"channel": "candle1m", "instId": inst},
                        "data": [
                            [
                                str(open_ts),
                                format(o, "f"),
                                format(h, "f"),
                                format(lo, "f"),
                                format(c, "f"),
                                format(vol, "f"),
                                format(vol * spec["ct_val"], "f"),
                                format(vol_quote, "f"),
                                confirm,
                            ]
                        ],
                    },
                    ts + 600,
                )
                if confirm == "1":
                    minute_trades[inst] = []
            if second % 60 == 0:
                funding_rate += Decimal(rng.randint(-2, 2)) / Decimal(1_000_000)
                push(
                    {
                        "arg": {"channel": "funding-rate", "instId": inst},
                        "data": [
                            {
                                "formulaType": "withRate",
                                "fundingRate": format(funding_rate, "f"),
                                "fundingTime": "1789747200000",
                                "impactValue": "1000",
                                "instId": inst,
                                "instType": "SWAP",
                                "interestRate": "0.0001",
                                "maxFundingRate": "0.00375",
                                "method": "current_period",
                                "minFundingRate": "-0.00375",
                                "nextFundingRate": "",
                                "nextFundingTime": "1789776000000",
                                "premium": "0.00005",
                                "settFundingRate": "0.00012",
                                "settState": "settled",
                                "ts": str(ts + 700),
                            }
                        ],
                    },
                    ts + 700,
                )
            if second == 120:
                push(sim.heartbeat(ts + 800), ts + 800)
    return write_jsonl_dataset(
        events,
        out_dir,
        dataset="golden-okx-usdt-swap-synthetic",
        clock=clock,
        quality_level="synthetic",
        notes=(
            "Jeu synthétique déterministe (scripts/build_golden_dataset.py, graine 25200) reproduisant les formes "
            "documentées OKX : carnets books séquencés (seqId non consécutifs, un maintien), trades (side taker), "
            "bougies 1m cohérentes avec les trades (confirm 0 puis 1), mark/index, tickers (volCcy24h en base), "
            "open interest, funding. Aucune statistique de marché réelle."
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "golden"))
    args = parser.parse_args()
    manifest = build(Path(args.out))
    print(json.dumps({k: v for k, v in manifest.items() if k != "notes"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

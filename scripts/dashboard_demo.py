"""Démonstration du tableau de bord sur un marché SYNTHÉTIQUE (aucune donnée réelle, aucun ordre).

Le vrai moteur (LiveEngine, courtier papier, stops, contrôles pré-enregistrés) tourne plusieurs jours de marché
synthétique, recalés pour finir maintenant, et écrit son état dans ``<dossier>/paper`` ; le tableau de bord le
sert ensuite, avec des bougies tirées du même marché synthétique. Sert au développement et aux tests visuels.

    python scripts/dashboard_demo.py build /tmp/hermes-demo --days 6
    python scripts/dashboard_demo.py serve /tmp/hermes-demo --port 8898
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.config import load_config
from hermes.data.live_feed import BinanceLiveFeed, DailyHistory
from hermes.data.panel import Panel
from hermes.data.synthetic import make_synthetic_panel
from hermes.execution.broker import PaperBroker
from hermes.live.engine import LiveEngine
from hermes.live.state import StateStore
from hermes.research.dataset import build_dataset
from hermes.research.run import train_final

NAMES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
    "SUIUSDT", "1000PEPEUSDT", "LTCUSDT", "DOTUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
]  # fmt: skip
LEVELS = [98_000, 3_600, 210, 2.9, 0.24, 640, 0.85, 34, 23, 3.4, 0.011, 110, 6.5, 5.2, 9.8, 0.75]
BPD = 96


def demo_config():
    return load_config(
        None,
        **{
            "data.bar": "15m",
            "data.universe.top_n": 14,
            "data.universe.min_history_days": 3,
            "data.universe.venue": "any",
            "model.gbm.seeds": [1],
            "model.gbm.n_estimators": 200,
            "model.gbm.learning_rate": 0.08,
            "model.gbm.min_data_in_leaf": 500,
            "validation.min_train_days": 20,
            "validation.test_days": 7,
            "validation.val_days": 5,
            "validation.train_sample_minutes": 30,
            "portfolio.holding_horizon": 8,
            "risk.stop_loss_daily_sigmas": 3.0,  # tighter than production so that a few stops show up
        },
    )


def demo_panel(n_days: int) -> Panel:
    p = make_synthetic_panel(n_assets=len(NAMES), n_bars=BPD * n_days, bar="15m", seed=21, signal_strength=1.5)
    ren = dict(zip(p.symbols, NAMES))
    fields = {}
    first = p["close"].bfill().iloc[0]
    scale = pd.Series(LEVELS, index=p.symbols) / first
    for k, v in p.fields.items():
        v = v.rename(columns=ren)
        if k in ("open", "high", "low", "close"):
            v = v * scale.rename(index=ren)
        elif k == "volume":
            v = v / scale.rename(index=ren)
        fields[k] = v
    return Panel(fields, bar="15m")


class DemoFeed:
    """The synthetic market shifted so that its bar ``t_end - 1`` closes now; ``t`` advances the replay."""

    def __init__(self, panel: Panel, t_end: int, window: int):
        shift = BinanceLiveFeed.last_closed_bar("15m") - panel.index[t_end - 1]
        self.panel = Panel({k: v.set_axis(v.index + shift) for k, v in panel.fields.items()}, bar="15m")
        self.t, self.window = t_end, window

    async def top_symbols(self, n):
        return self.panel.symbols[:n]

    async def update(self, symbols):
        return self.panel.iloc(slice(self.t - self.window, self.t)).subset(symbols)

    async def daily(self, symbols):
        p = self.panel.iloc(slice(0, self.t)).subset(symbols)
        today = p.index[-1].floor("D")
        qv = p["quote_volume"].resample("1D").sum()
        close = p["close"].resample("1D").last()
        keep = qv.index < today
        return DailyHistory(qv[keep], close[keep].notna(), close[keep])


def build(out: Path, days: int) -> None:
    cfg = demo_config()
    train_days, total = 40, 40 + days + 32
    panel = demo_panel(total)
    ds = build_dataset(panel.iloc(slice(0, BPD * train_days)), cfg)
    bundle = train_final(ds, cfg, promoted=False, evaluation={})
    bundle.prior_ic = 0.05
    bundle.meta["cost_scale"] = 0.35
    bundle.meta["research"] = {
        "name": "DÉMO synthétique",
        "bar": "15m",
        "horizon_bars": 8,
        "sharpe": 1.1,
        "cagr": 0.126,
        "vol": 0.11,
        "max_drawdown": -0.11,
        "ic": 0.045,
    }
    bundle.meta["gate"] = {
        "dsr": {"value": 0.46, "threshold": 0.95, "pass": False},
        "null_pvalue": {"value": 0.04, "threshold": 0.05, "pass": True},
        "pbo": {"value": 0.38, "threshold": 0.3, "pass": False},
        "sharpe": {"value": 1.1, "threshold": 0.8, "pass": True},
        "positive_years": {"value": 0.75, "threshold": 0.6, "pass": True},
        "oos_months": {"value": 37, "threshold": 12, "pass": True},
        "cost_stress": {"value": 0.64, "threshold": 0.0, "pass": True},
        "latency_stress": {"value": 1.08, "threshold": 0.0, "pass": True},
        "stop_stress": {"value": 0.39, "threshold": 0.0, "pass": True},
    }
    state = out / "paper"
    state.mkdir(parents=True, exist_ok=True)
    store = StateStore(state)
    broker = PaperBroker(state / "paper_account.json", 10_000, 0.0002, 0.0005)
    t_end = BPD * (train_days + days + 30)
    t0 = t_end - BPD * days
    feed = DemoFeed(panel, t_end, BPD * 30)
    cfg = cfg.model_copy(update={"live": cfg.live.model_copy(update={"state_dir": out})})
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    bar = pd.Timedelta(minutes=15)
    last_fill = last_event = 0
    started = time.time()
    for t in range(t0, t_end + 1):
        feed.t = t
        sim_now = feed.panel.index[t - 1] + bar + pd.Timedelta(seconds=8)
        eng.clock = lambda sim_now=sim_now: sim_now
        eng.last_cycle_s = round(4 + 3 * np.random.default_rng(t).random(), 2)
        asyncio.run(eng.step())
        stamp = sim_now.timestamp()
        with sqlite3.connect(state / "hermes.sqlite3") as db:  # executions at the simulated time, not now
            db.execute("UPDATE fills SET ts = ? WHERE id > ?", (stamp, last_fill))
            db.execute("UPDATE events SET ts = ? WHERE id > ?", (stamp, last_event))
            last_fill = db.execute("SELECT COALESCE(MAX(id), 0) FROM fills").fetchone()[0]
            last_event = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        if (t - t0) % BPD == 0:
            print(f"jour {(t - t0) // BPD}/{days} ({time.time() - started:.0f} s)", flush=True)
    store.event("INFO", "Démonstration : marché synthétique, aucun ordre réel")
    with open(out / "demo_market.pkl", "wb") as fh:
        pickle.dump(
            {k: v for k, v in feed.panel.fields.items() if k in ("open", "high", "low", "close", "quote_volume")}, fh
        )
    print(json.dumps({"state": str(state), "fills": last_fill, "seconds": round(time.time() - started)}))


def demo_candles(out: Path):
    with open(out / "demo_market.pkl", "rb") as fh:
        m = pickle.load(fh)
    rules = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1D"}

    def source(symbol: str, tf: str, limit: int) -> list[list[float]]:
        if symbol not in m["close"]:
            raise KeyError(symbol)
        now = pd.Timestamp.now(tz="UTC")
        df = pd.DataFrame({k: m[k][symbol] for k in ("open", "high", "low", "close", "quote_volume")})
        df = df[df.index <= now].dropna()
        if tf != "5m":
            df = (
                df.resample(rules[tf])
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "quote_volume": "sum"})
                .dropna()
            )
        df = df.iloc[-limit:]
        return [[int(t.timestamp()), *map(float, r)] for t, r in zip(df.index, df.to_numpy())]

    return source


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["build", "serve"])
    ap.add_argument("out", type=Path)
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--port", type=int, default=8898)
    a = ap.parse_args()
    if a.action == "build":
        build(a.out, a.days)
    else:
        from hermes.live.dashboard import serve

        print(f"http://127.0.0.1:{a.port}/", file=sys.stderr)
        serve(a.out, "127.0.0.1", a.port, candles=demo_candles(a.out))


if __name__ == "__main__":
    main()

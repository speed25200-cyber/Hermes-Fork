"""Durable engine state (SQLite): decisions, scores, fills, equity, events, risk state.

Everything the engine needs to restart where it stopped, and everything an operator needs to audit what
it did, lives here. Writes are small and transactional.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (ts TEXT, symbol TEXT, score REAL, PRIMARY KEY (ts, symbol));
CREATE TABLE IF NOT EXISTS decisions (ts TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, side TEXT, qty REAL,
                                  price REAL, fee REAL, maker INTEGER);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, gross REAL, net REAL, drawdown REAL,
                                   ic_est REAL, n_positions INTEGER);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class StateStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "hermes.sqlite3")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- key/value --------------------------------------------------------------------------------------------
    def get(self, key: str, default: object = None) -> object:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: object) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value, default=str)))
        self.db.commit()

    # -- records ----------------------------------------------------------------------------------------------
    def add_scores(self, ts: pd.Timestamp, scores: pd.Series) -> None:
        rows = [(ts.isoformat(), s, float(v)) for s, v in scores.items() if pd.notna(v)]
        self.db.executemany("INSERT OR REPLACE INTO scores VALUES (?, ?, ?)", rows)
        self.db.commit()

    def score_history(self, since: pd.Timestamp) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT ts, symbol, score FROM scores WHERE ts >= ?", self.db, params=(since.isoformat(),)
        )
        if df.empty:
            return pd.DataFrame()
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df.pivot(index="ts", columns="symbol", values="score")

    def add_decision(self, ts: pd.Timestamp, payload: dict[str, object]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?, ?)", (ts.isoformat(), json.dumps(payload, default=str))
        )
        self.db.commit()

    def add_fills(self, fills: list) -> None:  # type: ignore[type-arg]
        self.db.executemany(
            "INSERT INTO fills (ts, symbol, side, qty, price, fee, maker) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(f.ts, f.symbol, f.side, f.qty, f.price, f.fee, int(f.maker)) for f in fills],
        )
        self.db.commit()

    def add_equity(
        self,
        ts: pd.Timestamp,
        equity: float,
        gross: float,
        net: float,
        drawdown: float,
        ic_est: float,
        n_positions: int,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), equity, gross, net, drawdown, ic_est, n_positions),
        )
        self.db.commit()

    def event(self, level: str, message: str) -> None:
        self.db.execute("INSERT INTO events (ts, level, message) VALUES (?, ?, ?)", (time.time(), level, message))
        self.db.commit()

    def recent_events(self, n: int = 20) -> list[tuple[float, str, str]]:
        return list(self.db.execute("SELECT ts, level, message FROM events ORDER BY id DESC LIMIT ?", (n,)))

    def equity_curve(self) -> pd.DataFrame:
        df = pd.read_sql_query("SELECT * FROM equity ORDER BY ts", self.db)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df = df.set_index("ts")
        return df

    def write_status(self, status: dict[str, object]) -> None:
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(status, indent=1, default=str))
        tmp.replace(self.dir / "status.json")

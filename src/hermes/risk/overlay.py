"""Portfolio-level risk overlay, applied identically in backtest, paper and live.

Layers, from slowest to fastest:

1. **Drawdown control** (Grossman & Zhou 1993 style): risk budget is 1 until ``drawdown_soft``, then falls
   linearly to 0 at ``drawdown_hard``, where everything is flattened and the engine halts until a human
   resets it. This bounds the worst-case loss by construction, whatever the model does.
2. **Daily loss breaker**: once the UTC-day loss exceeds ``daily_loss_limit``, no position may grow until the
   next UTC day (reductions stay allowed).
3. **Expected-shortfall cap**: the 1-day 97.5% ES of the proposed book (max of a fat-tailed parametric
   estimate and a historical simulation on the last months) must stay under ``es_limit_daily``.
4. **Kill switch**: a file on disk (or an automatic trigger) flattens the book immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.config import RiskConfig

ES_Z_975 = 2.338  # E[Z | Z > 1.96] for a standard normal
FAT_TAIL = 1.4  # crypto tails: empirical 1-day ES is ~1.3-1.5x the normal one


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: object = None
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    events: list[tuple[str, str]] = field(default_factory=list)
    last_equity: float = 0.0  # equity at the previous observation (the start of a new UTC day)


class RiskOverlay:
    def __init__(self, cfg: RiskConfig, state: RiskState | None = None, check_kill_file: bool = True):
        """``check_kill_file`` is off in backtests: an operator's kill file must never alter research."""
        self.cfg = cfg
        self.state = state or RiskState()
        self.check_kill_file = check_kill_file

    # -- state -----------------------------------------------------------------------------------------------
    def observe(self, ts: pd.Timestamp, equity: float, day: object = None) -> None:
        """``day`` may be passed pre-computed (any hashable UTC-day key) to avoid per-bar date arithmetic."""
        s = self.state
        if equity > s.peak_equity:
            s.peak_equity = equity
        day = ts.floor("D") if day is None else day
        if s.day != day:
            # The day starts at the previous close: the first bar's own P&L counts toward the daily loss.
            s.day = day
            s.day_start_equity = s.last_equity if s.last_equity > 0 else equity
        s.last_equity = equity
        if not s.halted and self.drawdown(equity) >= self.cfg.drawdown_hard:
            self.halt(ts, f"drawdown de {self.drawdown(equity):.1%} au-delà de la limite d'arrêt")

    def halt(self, ts: pd.Timestamp, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self.state.events.append((str(ts), f"HALT: {reason}"))

    def drawdown(self, equity: float) -> float:
        p = self.state.peak_equity
        return 0.0 if p <= 0 else max(0.0, 1.0 - equity / p)

    def daily_loss(self, equity: float) -> float:
        d = self.state.day_start_equity
        return 0.0 if d <= 0 else max(0.0, 1.0 - equity / d)

    def kill_requested(self) -> bool:
        return self.check_kill_file and Path(self.cfg.kill_switch_file).exists()

    # -- decisions -------------------------------------------------------------------------------------------
    def budget(self, equity: float) -> float:
        """Multiplier in [0, 1] on the target book, proportional to the remaining drawdown cushion (Grossman &
        Zhou): the hard limit is approached asymptotically, so a deep drawdown leaves the book nearly idle
        rather than formally halted -- ``cushion_exhausted`` reports that state."""
        if self.state.halted:
            return 0.0
        dd = self.drawdown(equity)
        if dd <= self.cfg.drawdown_soft:
            return 1.0
        return float(np.clip((self.cfg.drawdown_hard - dd) / (self.cfg.drawdown_hard - self.cfg.drawdown_soft), 0, 1))

    def cushion_exhausted(self, equity: float, threshold: float = 0.05) -> bool:
        """True when the drawdown budget has fallen below ``threshold``: the book is de facto stopped."""
        return self.budget(equity) < threshold

    def reduce_only(self, equity: float) -> bool:
        return self.daily_loss(equity) >= self.cfg.daily_loss_limit

    @staticmethod
    def restrict_to_reductions(target: np.ndarray, current: np.ndarray) -> np.ndarray:
        """Keep each position between zero and its current value (no increase, no flip)."""
        same = np.sign(target) == np.sign(current)
        out = np.where(same, np.sign(current) * np.minimum(np.abs(target), np.abs(current)), 0.0)
        return out

    def es_scale(
        self, w: np.ndarray, cov_bar: np.ndarray, bars_per_day: int, hist_daily_returns: np.ndarray | None = None
    ) -> tuple[float, float]:
        """Scale factor so that the 1-day 97.5% ES respects the limit; also returns the ES estimate."""
        var_day = float(w @ cov_bar @ w) * bars_per_day
        es_param = np.sqrt(max(var_day, 0.0)) * ES_Z_975 * FAT_TAIL
        es_hist = 0.0
        if hist_daily_returns is not None and len(hist_daily_returns) >= 60:
            pnl = hist_daily_returns @ w
            q = np.quantile(pnl, 0.025)
            tail = pnl[pnl <= q]
            es_hist = float(-tail.mean()) if len(tail) else 0.0
        es = max(es_param, es_hist)
        if es <= self.cfg.es_limit_daily or es <= 0:
            return 1.0, es
        return self.cfg.es_limit_daily / es, es

    def apply(
        self,
        ts: pd.Timestamp,
        equity: float,
        target: np.ndarray,
        current: np.ndarray,
        cov_bar: np.ndarray,
        bars_per_day: int,
        hist_daily_returns: np.ndarray | None = None,
    ) -> tuple[np.ndarray, dict[str, float]]:
        info: dict[str, float] = {}
        if self.kill_requested() and not self.state.halted:
            self.halt(ts, "interrupteur d'urgence posé")
        b = self.budget(equity)
        info["budget"] = b
        w = target * b
        s, es = self.es_scale(w, cov_bar, bars_per_day, hist_daily_returns)
        info["es_1d"] = es * s
        info["es_scale"] = s
        w = w * s
        if self.reduce_only(equity):
            w = self.restrict_to_reductions(w, current)
            info["reduce_only"] = 1.0
        # Positions beyond max_positions: keep the largest.
        nz = np.nonzero(w)[0]
        if len(nz) > self.cfg.max_positions:
            keep = nz[np.argsort(-np.abs(w[nz]))[: self.cfg.max_positions]]
            mask = np.zeros_like(w, dtype=bool)
            mask[keep] = True
            w = np.where(mask, w, 0.0)
        return w, info

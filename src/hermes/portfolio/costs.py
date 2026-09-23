"""Transaction-cost model shared by the optimiser, the backtest and the execution pre-trade checks.

Cost of trading ``q`` dollars of contract ``i`` (as a fraction of ``q``)::

    c_i(q) = fee_blend + (1 - maker_share) * half_spread_i + impact_coef * sigma_daily_i * sqrt(q / ADV_i)

* ``fee_blend = maker_share * maker_fee + (1 - maker_share) * taker_fee`` -- execution is maker-first, and
  only the taker share crosses the spread;
* the half-spread is estimated from high/low/close with Abdi & Ranaldo (2017), floored;
* impact follows the square-root law (Donier & Bonart 2015 measured a prefactor ~0.9 on BTC; 0.5-1 is
  typical of futures), in units of daily volatility.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from hermes.config import CostConfig


def abdi_ranaldo_half_spread(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, window: int) -> pd.DataFrame:
    """Causal rolling half-spread estimate (fraction of price). Uses pairs (t-1, t), known at close(t)."""
    eta = (np.log(high) + np.log(low)) / 2.0
    c = np.log(close)
    prod = (c.shift(1) - eta.shift(1)) * (c.shift(1) - eta)
    s2 = 4.0 * prod.rolling(window, min_periods=window // 3).mean()
    return np.sqrt(s2.clip(lower=0.0)) / 2.0


@dataclass
class CostModel:
    cfg: CostConfig
    half_spread: pd.DataFrame  # fraction of price, (time x symbol)
    sigma_daily: pd.DataFrame  # daily volatility (fraction)
    adv: pd.DataFrame  # average daily dollar volume

    @classmethod
    def from_panel(
        cls,
        cfg: CostConfig,
        high: pd.DataFrame,
        low: pd.DataFrame,
        close: pd.DataFrame,
        quote_volume: pd.DataFrame,
        vol_per_bar: pd.DataFrame,
        bars_per_day: int,
    ) -> CostModel:
        hs = abdi_ranaldo_half_spread(high, low, close, bars_per_day * 7)
        floor = cfg.min_half_spread_bps * 1e-4
        hs = hs.clip(lower=floor, upper=50e-4).fillna(10e-4)
        adv = quote_volume.rolling(bars_per_day * 14, min_periods=bars_per_day).mean() * bars_per_day
        return cls(cfg=cfg, half_spread=hs, sigma_daily=vol_per_bar * np.sqrt(bars_per_day), adv=adv)

    @property
    def fee_blend(self) -> float:
        m = self.cfg.maker_fill_ratio
        return m * self.cfg.maker_fee + (1 - m) * self.cfg.taker_fee

    def __post_init__(self) -> None:
        # Numpy views for the per-bar hot path (the backtest calls these tens of thousands of times).
        self._hs = np.nan_to_num(self.half_spread.to_numpy(dtype=np.float64), nan=10e-4)
        self._sig = np.nan_to_num(self.sigma_daily.to_numpy(dtype=np.float64), nan=0.05)
        self._adv = self.adv.to_numpy(dtype=np.float64)

    def adv_at(self, t: int) -> np.ndarray:
        return self._adv[t]

    def linear_rate(self, t: int, trade_dollars: np.ndarray | float = 0.0) -> np.ndarray:
        """Per-unit cost rate for each contract at bar position ``t`` for a typical trade size."""
        q = np.abs(np.asarray(trade_dollars, float))
        adv = self._adv[t]
        part = np.where(np.isfinite(adv) & (adv > 0), q / np.where(adv > 0, adv, 1.0), 1.0)
        impact = self.cfg.impact_coef * self._sig[t] * np.sqrt(part)
        return self.fee_blend + (1 - self.cfg.maker_fill_ratio) * self._hs[t] + impact

    def trade_cost(self, t: int, dollars: np.ndarray) -> tuple[float, float, float]:
        """Total cost of trading ``dollars`` (signed) at bar ``t``: (fees, spread, impact) in dollars."""
        q = np.abs(dollars)
        m = self.cfg.maker_fill_ratio
        fees = float(np.sum(q) * self.fee_blend)
        spread = float(np.sum(q * (1 - m) * self._hs[t]))
        adv = self._adv[t]
        part = np.where(np.isfinite(adv) & (adv > 0), q / np.where(adv > 0, adv, 1.0), 1.0)
        impact = float(np.sum(q * self.cfg.impact_coef * self._sig[t] * np.sqrt(part)))
        return fees, spread, impact

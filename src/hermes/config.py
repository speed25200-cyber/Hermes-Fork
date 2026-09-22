"""Typed configuration for every layer of Hermes.

A single YAML file (``configs/*.yaml``) maps onto :class:`HermesConfig`. Unknown keys are rejected so a
typo can never silently fall back to a default. Every default below is a deliberate, documented choice;
see ``docs/ARCHITECTURE.md`` for the reasoning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseConfig(_Strict):
    """Point-in-time universe: the N most liquid perpetuals *as of each date*, never chosen with hindsight."""

    top_n: int = Field(40, ge=2, le=500)
    liquidity_lookback_days: int = Field(30, ge=1)
    min_history_days: int = Field(60, ge=1, description="Listing age before a contract may enter")
    reselect_every_days: int = Field(7, ge=1)
    quote: str = "USDT"
    exclude: tuple[str, ...] = ("USDCUSDT", "BTCDOMUSDT", "DEFIUSDT", "FDUSDUSDT", "TUSDUSDT")
    symbols: tuple[str, ...] = Field((), description="Explicit symbol list; empty = discover from archive")


class DataConfig(_Strict):
    source: Literal["binance_archive", "synthetic"] = "binance_archive"
    cache_dir: Path = Path("data")
    bar: Literal["15m", "30m", "1h", "2h", "4h"] = "1h"
    start: str = "2021-01-01"
    end: str | None = None
    include_metrics: bool = False
    include_premium: bool = True
    download_workers: int = Field(16, ge=1, le=64)
    universe: UniverseConfig = UniverseConfig()


class FeatureConfig(_Strict):
    return_windows: tuple[int, ...] = (1, 2, 4, 8, 12, 24, 48, 72, 168, 336)
    vol_windows: tuple[int, ...] = (24, 72, 168)
    flow_windows: tuple[int, ...] = (1, 4, 12, 24, 72)
    funding_windows: tuple[int, ...] = (8, 24, 72, 168)
    vol_halflife: int = Field(72, ge=2, description="EWMA half-life (bars) of the ex-ante volatility")
    cross_sectional: bool = True
    market_features: bool = True
    time_features: bool = True
    max_lookback: int = Field(720, ge=24, description="Bars of history needed to compute every feature")


class LabelConfig(_Strict):
    horizons: tuple[int, ...] = (4, 8, 24)
    primary_horizon: int = 8
    residualize: Literal["none", "mean", "beta"] = "beta"
    vol_normalize: bool = True
    clip_sigma: float = Field(5.0, gt=0)

    @model_validator(mode="after")
    def _primary_in_horizons(self) -> LabelConfig:
        if self.primary_horizon not in self.horizons:
            raise ValueError("primary_horizon must be one of horizons")
        return self


class GBMConfig(_Strict):
    enabled: bool = True
    n_estimators: int = 2000
    learning_rate: float = 0.02
    num_leaves: int = 31
    min_data_in_leaf: int = 2000
    feature_fraction: float = 0.6
    bagging_fraction: float = 0.7
    bagging_freq: int = 1
    lambda_l2: float = 10.0
    max_bin: int = 63
    early_stopping_rounds: int = 150
    seeds: tuple[int, ...] = (1, 2, 3)
    objective: Literal["regression", "huber"] = "huber"
    n_jobs: int = 0


class LinearConfig(_Strict):
    enabled: bool = True
    alpha: float = 300.0


class DeepConfig(_Strict):
    enabled: bool = False
    seq_len: int = 48
    d_model: int = 64
    n_heads: int = 4
    n_layers: int = 2
    dropout: float = 0.15
    epochs: int = 12
    batch_timestamps: int = 64
    lr: float = 1e-3
    weight_decay: float = 1e-4
    seed: int = 7
    patience: int = 3


class ModelConfig(_Strict):
    gbm: GBMConfig = GBMConfig()
    linear: LinearConfig = LinearConfig()
    deep: DeepConfig = DeepConfig()
    ensemble: Literal["ic_weighted", "equal"] = "ic_weighted"
    market_model: bool = True


class ValidationConfig(_Strict):
    train_bars: int = Field(24 * 365, ge=100, description="Rolling training window; 0 = expanding")
    expanding: bool = True
    test_bars: int = Field(24 * 30, ge=24)
    embargo_bars: int = Field(24, ge=0)
    min_train_bars: int = Field(24 * 180, ge=100)
    val_bars: int = Field(
        24 * 60, ge=24, description="Most recent part of each training window used for early stopping"
    )
    train_stride: int = Field(2, ge=1, description="Keep one training bar in N (labels overlap anyway)")
    recency_halflife_days: float = Field(0.0, ge=0, description="Sample-weight half-life; 0 = equal weights")
    cpcv_groups: int = Field(8, ge=4)
    cpcv_test_groups: int = Field(2, ge=1)
    bootstrap_samples: int = Field(2000, ge=100)
    null_permutations: int = Field(200, ge=20)
    n_trials: int = Field(1, ge=1, description="Configurations tried so far (feeds the Deflated Sharpe Ratio)")
    # Promotion gate: a strategy trades real money only if ALL of these hold out of sample, net of costs.
    gate_min_dsr: float = 0.95
    gate_min_null_percentile: float = 0.95
    gate_max_pbo: float = 0.30
    gate_min_sharpe: float = 0.8
    gate_min_positive_year_fraction: float = 0.6
    gate_min_months: int = 12


class CostConfig(_Strict):
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    maker_fill_ratio: float = Field(0.6, ge=0, le=1, description="Share of traded notional filled passively")
    min_half_spread_bps: float = 1.0
    impact_coef: float = Field(0.7, ge=0, description="Square-root impact: coef * sigma_daily * sqrt(q/ADV)")
    include_funding: bool = True


class PortfolioConfig(_Strict):
    vol_target_annual: float = Field(0.20, gt=0, le=2.0)
    gross_max: float = Field(2.5, gt=0)
    net_max: float = Field(0.25, ge=0)
    weight_max: float = Field(0.20, gt=0)
    adv_participation_max: float = Field(0.02, gt=0, le=1)
    beta_neutral: bool = True
    rebalance_every: int = Field(1, ge=1)
    cost_aversion: float = Field(1.0, ge=0, description="Multiplier on expected trading costs in the optimiser")
    holding_horizon: int = Field(8, ge=1, description="Bars over which a forecast is expected to be earned")
    cov_halflife: int = Field(24 * 14, ge=24)
    cov_min_periods: int = Field(24 * 7, ge=24)
    min_trade_weight: float = Field(0.002, ge=0)
    ic_ref: float = Field(0.03, gt=0, description="IC at which the book reaches its volatility target")


class RiskConfig(_Strict):
    daily_loss_limit: float = Field(0.03, gt=0, description="UTC-day loss that freezes new risk")
    drawdown_soft: float = Field(0.10, gt=0, description="Drawdown where de-risking starts")
    drawdown_hard: float = Field(0.25, gt=0, description="Drawdown where everything is flattened and halted")
    es_limit_daily: float = Field(0.04, gt=0, description="Max 1-day 97.5% expected shortfall")
    max_positions: int = Field(40, ge=1)
    exchange_leverage: int = Field(5, ge=1, le=50)
    stop_loss_daily_sigmas: float = Field(4.0, gt=0, description="Server-side catastrophe stop distance")
    max_data_staleness_s: int = Field(900, ge=30)
    kill_switch_file: Path = Path("state/KILL")

    @model_validator(mode="after")
    def _ordered(self) -> RiskConfig:
        if self.drawdown_soft >= self.drawdown_hard:
            raise ValueError("drawdown_soft must be below drawdown_hard")
        return self


class ExecutionConfig(_Strict):
    venue: Literal["okx", "paper"] = "paper"
    okx_base_url: str = "https://www.okx.com"
    okx_demo: bool = True
    td_mode: Literal["cross", "isolated"] = "cross"
    maker_first: bool = True
    maker_timeout_s: float = Field(90.0, ge=1)
    chase_interval_s: float = Field(10.0, ge=0.5)
    max_chases: int = Field(8, ge=0)
    taker_slippage_cap_bps: float = Field(15.0, ge=0)
    child_max_book_fraction: float = Field(0.5, gt=0, le=1)
    dead_man_switch_s: int = Field(60, ge=10, le=120)
    order_tag: str = "hermes"


class LiveConfig(_Strict):
    state_dir: Path = Path("state")
    model_dir: Path = Path("artifacts/models/champion")
    bar_close_delay_s: float = Field(20.0, ge=0)
    capital_fraction: float = Field(1.0, gt=0, le=1, description="Share of account equity the engine may use")
    paper_initial_equity: float = 10_000.0
    history_bars: int = Field(2400, ge=500, description="Bars of history kept by the live feed")
    candidates: int = Field(80, ge=5, description="Most traded contracts considered each day")
    allow_unpromoted: bool = Field(False, description="DANGER: trade real money with a model that failed the gate")


class HermesConfig(_Strict):
    name: str = "hermes"
    seed: int = 42
    data: DataConfig = DataConfig()
    features: FeatureConfig = FeatureConfig()
    labels: LabelConfig = LabelConfig()
    model: ModelConfig = ModelConfig()
    validation: ValidationConfig = ValidationConfig()
    costs: CostConfig = CostConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    live: LiveConfig = LiveConfig()

    @property
    def bars_per_day(self) -> int:
        return {"15m": 96, "30m": 48, "1h": 24, "2h": 12, "4h": 6}[self.data.bar]

    @property
    def bars_per_year(self) -> float:
        return self.bars_per_day * 365.0


def load_config(path: str | Path | None = None, **overrides: object) -> HermesConfig:
    """Load a YAML config (or defaults) and apply dotted overrides, e.g. ``{"data.bar": "4h"}``."""
    raw: dict[str, object] = {}
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    for dotted, value in overrides.items():
        node: dict[str, object] = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})  # type: ignore[assignment]
        node[leaf] = value
    return HermesConfig.model_validate(raw)

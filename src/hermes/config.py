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
    venue: Literal["any", "okx"] = Field(
        "okx",
        description="Execution venue: only contracts it listed at the time may enter (the live engine can trade "
        "nothing else); 'any' ranks every Binance contract",
    )


BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
Bar = Literal["1m", "5m", "15m", "30m", "1h", "2h", "4h"]


def bars_for(minutes: float, bar: str) -> int:
    """Number of bars spanning ``minutes`` (at least one)."""
    return max(1, round(minutes / BAR_MINUTES[bar]))


class DataConfig(_Strict):
    source: Literal["binance_archive", "synthetic"] = "binance_archive"
    cache_dir: Path = Path("data")
    bar: Bar = "15m"
    start: str = "2022-01-01"
    end: str | None = None
    include_metrics: bool = False
    include_premium: bool = True
    intrabar: bool = Field(False, description="Add 1-minute microstructure aggregates to each base bar")
    intrabar_start: str | None = Field(None, description="First date of 1-minute aggregates (default: start)")
    source_bar: Bar | None = Field(None, description="Download this bar and aggregate it to `bar` (e.g. 15m -> 30m)")
    download_workers: int = Field(16, ge=1, le=64)
    universe: UniverseConfig = UniverseConfig()


class FeatureConfig(_Strict):
    """Feature windows are expressed in **minutes** and converted to bars of the configured timeframe, so the
    same definitions serve 1-minute, 15-minute and 30-minute models. Windows shorter than one bar collapse
    to one bar (duplicates are dropped)."""

    return_minutes: tuple[int, ...] = (15, 30, 60, 120, 240, 480, 1440, 4320, 10080)
    vol_minutes: tuple[int, ...] = (240, 1440, 4320)
    flow_minutes: tuple[int, ...] = (15, 60, 240, 1440)
    funding_minutes: tuple[int, ...] = (480, 1440, 4320)
    trend_pairs_minutes: tuple[tuple[int, int], ...] = ((60, 240), (240, 1440), (1440, 5760))
    range_minutes: tuple[int, ...] = (240, 1440, 10080)
    day_minutes: int = Field(1440, ge=1, description="'Daily' windows: trade size, spread proxy, close location")
    long_minutes: int = Field(10080, ge=1, description="Long windows: skew, kurtosis, semi-variance, Amihud")
    zscore_minutes: int = Field(20160, ge=1, description="History used to z-score funding and premium")
    vol_halflife_minutes: int = Field(1440, ge=2, description="EWMA half-life of the ex-ante volatility")
    max_lookback_minutes: int = Field(20160, ge=60, description="History needed to compute every feature")
    cross_sectional: bool = True
    market_features: bool = True
    time_features: bool = True
    intrabar: bool = Field(True, description="Use 1-minute aggregates when the panel carries them")


class LabelConfig(_Strict):
    """Horizons in bars of the configured timeframe (15m bars: 2, 4, 8 = 30 min, 1 h, 2 h)."""

    horizons: tuple[int, ...] = (2, 4, 8)
    primary_horizon: int = 4
    residualize: Literal["none", "mean", "beta", "style"] = "beta"
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
    """Windows in days (converted to bars of the configured timeframe)."""

    train_days: float = Field(365, gt=0, description="Rolling training window when not expanding")
    expanding: bool = True
    test_days: float = Field(90, gt=0, description="Refit period: each model predicts this many days")
    embargo_minutes: float = Field(1440, ge=0)
    min_train_days: float = Field(365, gt=0)
    val_days: float = Field(60, gt=0, description="Most recent part of each training window used for early stopping")
    train_sample_minutes: float = Field(60, gt=0, description="Keep one training bar per this many minutes")
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
    rebalance_every: int = Field(
        1, ge=1, description="Decide every k bars, aligned on the clock (bar count since 1970), in research and live"
    )
    cost_aversion: float = Field(1.0, ge=0, description="Multiplier on expected trading costs in the optimiser")
    holding_horizon: int = Field(4, ge=1, description="Bars over which a forecast is expected to be earned")
    cov_halflife_days: float = Field(7, gt=0, description="EWMA half-life of the sample covariance")
    min_trade_weight: float = Field(0.002, ge=0)
    ic_ref: float = Field(0.03, gt=0, description="IC at which the book reaches its volatility target")
    min_position_usdt: float = Field(5.0, ge=0, description="Positions smaller than this are not held")
    signal_halflife: float = Field(
        0.0, ge=0, description="EWMA half-life of the traded score, in holding horizons (0 = raw model score)"
    )
    cost_scale_floor: float = Field(
        0.2, gt=0, le=1, description="Lowest cost amortisation factor 1 - rho_H (1 = no amortisation)"
    )
    style_neutral: bool = Field(
        False, description="Price size/liquidity and volatility exposures as risk (neutralises style bets)"
    )
    style_risk: float = Field(
        1.0, gt=0, description="Variance of one unit of style exposure, in units of the market variance"
    )
    regime_gate_drawdown: float = Field(
        0.0,
        ge=0,
        lt=1,
        description="Scale the book down while BTC closes this far below its high (0 = off): cross-sectional "
        "ICs were lower in past BTC drawdowns",
    )
    regime_gate_lookback_days: int = Field(90, ge=5, description="Window of the high the drawdown is taken from")
    regime_gate_scale: float = Field(0.5, ge=0, le=1, description="Multiplier of the IC estimate in the regime")
    regime_gate_symbol: str = "BTCUSDT"


class RiskConfig(_Strict):
    daily_loss_limit: float = Field(0.03, gt=0, description="UTC-day loss that freezes new risk")
    drawdown_soft: float = Field(0.10, gt=0, description="Drawdown where de-risking starts")
    drawdown_hard: float = Field(0.25, gt=0, description="Drawdown where everything is flattened and halted")
    es_limit_daily: float = Field(0.04, gt=0, description="Max 1-day 97.5% expected shortfall")
    max_positions: int = Field(40, ge=1)
    exchange_leverage: int = Field(5, ge=1, le=50)
    stop_loss_daily_sigmas: float = Field(
        8.0,
        gt=0,
        description="Server-side catastrophe stop distance from the entry price, in daily sigmas (3 %-50 %): "
        "a protection for when the engine is down, not a trading rule -- at 4 sigmas it fired on a third of "
        "all days and its simulated fills drove the P&L",
    )
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
    maker_timeout_s: float = Field(60.0, ge=1)
    chase_interval_s: float = Field(5.0, ge=0.5)
    max_chases: int = Field(8, ge=0)
    taker_slippage_cap_bps: float = Field(15.0, ge=0)
    child_max_book_fraction: float = Field(0.5, gt=0, le=1)
    dead_man_switch_s: int = Field(60, ge=10, le=120)
    order_tag: str = "hermes"


class LiveConfig(_Strict):
    state_dir: Path = Path("state")
    model_dir: Path = Path("artifacts/models/champion")
    bar_close_delay_s: float = Field(3.0, ge=0, description="Wait after the bar close before deciding")
    capital_fraction: float = Field(1.0, gt=0, le=1, description="Share of account equity the engine may use")
    paper_initial_equity: float = 10_000.0
    history_days: float | None = Field(
        None, gt=0, description="Minimum base-bar history kept by the live feed (default: the feature warm-up)"
    )
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
    def bar_minutes(self) -> int:
        return BAR_MINUTES[self.data.bar]

    @property
    def bars_per_day(self) -> int:
        return 1440 // self.bar_minutes

    @property
    def bars_per_year(self) -> float:
        return self.bars_per_day * 365.0

    def bars(self, minutes: float) -> int:
        """Bars of the configured timeframe spanning ``minutes`` (at least one)."""
        return bars_for(minutes, self.data.bar)

    def days(self, days: float) -> int:
        return max(1, round(days * self.bars_per_day))


def _set_dotted(raw: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    for dotted, value in overrides.items():
        node: dict[str, object] = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})  # type: ignore[assignment]
        node[leaf] = value
    return raw


def with_overrides(cfg: HermesConfig, overrides: dict[str, object]) -> HermesConfig:
    """A copy of ``cfg`` with dotted overrides applied (validated like a config file)."""
    if not overrides:
        return cfg
    return HermesConfig.model_validate(_set_dotted(cfg.model_dump(mode="json"), overrides))


def load_config(path: str | Path | None = None, **overrides: object) -> HermesConfig:
    """Load a YAML config (or defaults) and apply dotted overrides, e.g. ``{"data.bar": "4h"}``."""
    raw: dict[str, object] = {}
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    return HermesConfig.model_validate(_set_dotted(raw, overrides))

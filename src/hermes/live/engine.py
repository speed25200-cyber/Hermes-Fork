"""The live decision loop.

Once per bar, a few seconds after the close:

1. refresh the Binance feed (closed bars only) and check freshness -- stale data means **no new risk**;
2. recompute the point-in-time universe and the features with the research code, on the live window;
3. score the members with the model bundle (feature names must match the bundle exactly);
4. update the realised-IC tracker and the causal IC estimate that sizes the book;
5. build the target book with the same constructor as the backtest, then the risk overlay;
6. send the difference to the broker (maker-first), refresh exchange-side catastrophe stops;
7. persist everything (scores, decision, fills, equity, risk state) and publish a status file.

Modes: ``paper`` (simulated fills on live prices), ``demo`` (OKX demo account), ``live`` (real money).
``live`` refuses to open risk with a bundle that did not pass the promotion gate unless the operator
explicitly set ``live.allow_unpromoted``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.backtest.engine import is_rebalance_bar
from hermes.config import HermesConfig, with_overrides
from hermes.data.live_feed import BinanceLiveFeed, DailyHistory
from hermes.data.panel import BAR_TO_OFFSET, Panel
from hermes.data.universe import is_excluded, universe_mask
from hermes.data.venue import OkxListing
from hermes.execution.broker import Broker, Fill, PaperBroker, Position
from hermes.execution.okx.instruments import okx_inst_id
from hermes.features.library import STORAGE_DTYPE, FeatureSet, build_features, feature_warmup_bars
from hermes.labels.targets import build_targets
from hermes.live.alerts import alert
from hermes.live.listing_sleeve import HEDGE, ListingSleeve
from hermes.live.state import StateStore
from hermes.models.bundle import ModelBundle
from hermes.models.drift import DRIFT_MARKET_DAYS, drift_report
from hermes.portfolio.alpha import (
    cs_zscore,
    estimate_ic,
    market_alpha_series,
    regime_scale,
    rowwise_corr,
    signal_persistence,
    smooth_scores,
)
from hermes.portfolio.construct import BookInputs, PortfolioConstructor
from hermes.portfolio.costs import ADV_DAYS, CostModel
from hermes.portfolio.covariance import EwmaCovariance, market_variance
from hermes.risk.overlay import RiskOverlay, RiskState

log = logging.getLogger(__name__)

# Days of scores and realised IC kept in the state store (the IC estimate and market timing read ~90 days).
SCORE_MEMORY_DAYS = 120


def _rank_ic(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """Per-row Spearman correlation over the cells where both frames are finite."""
    b = b.reindex(index=a.index, columns=a.columns)
    ok = a.notna() & b.notna()
    return rowwise_corr(a.where(ok).rank(axis=1), b.where(ok).rank(axis=1))


def live_config(
    strategy: HermesConfig, operator: HermesConfig, overrides: dict[str, object] | None = None
) -> HermesConfig:
    """The configuration a live engine runs with.

    The strategy (data, features, labels, portfolio, costs) is the bundle's -- what was validated is what
    trades; execution, live and risk settings belong to the operator. Explicit ``--set`` overrides are applied
    last (a deliberate operator choice). Execution timing is scaled so that an order cycle ends well inside
    one bar: on 1-minute bars the passive phase lasts seconds, not minutes.
    """
    cfg = strategy.model_copy(update={"execution": operator.execution, "live": operator.live, "risk": operator.risk})
    cfg = with_overrides(cfg, overrides or {})
    bar_s = cfg.bar_minutes * 60.0
    ex = cfg.execution.model_copy(
        update={
            "maker_timeout_s": min(cfg.execution.maker_timeout_s, 0.15 * bar_s),
            "chase_interval_s": max(0.5, min(cfg.execution.chase_interval_s, 0.03 * bar_s)),
        }
    )
    return cfg.model_copy(update={"execution": ex})


def live_history_bars(cfg: HermesConfig) -> int:
    """Base bars the live feed keeps: the research warm-up (so features equal the research values) plus a
    week (the drift check's window of market-level features, with two bars to spare: the feed starts a bar
    short) -- a day at 1-minute bars, whose week would weigh on a small server --, the covariance and volume
    windows, and never less than ``live.history_days`` when set."""
    warm = feature_warmup_bars(cfg)
    drift = cfg.days(DRIFT_MARKET_DAYS) + 2 if cfg.bar_minutes >= 15 else cfg.bars_per_day
    cov = 2 * cfg.days(cfg.portfolio.cov_halflife_days) + 1
    # Style exposures (book and target) use the full ADV window; without them the cost model's ADV is a mean
    # over whatever history is held, which keeps the 1-minute live window small.
    styles = cfg.portfolio.style_neutral or cfg.labels.residualize == "style"
    adv = cfg.days(ADV_DAYS) + cfg.bars_per_day if styles else 0
    floor = cfg.days(cfg.live.history_days) if cfg.live.history_days else 0
    return int(max(warm + drift, cov, adv, floor))


def intrabar_history_minutes(cfg: HermesConfig) -> int:
    """1-minute history needed for the intrabar fields over the whole base-bar window."""
    if not cfg.data.intrabar or cfg.bar_minutes <= 1:
        return 0
    return live_history_bars(cfg) * cfg.bar_minutes + 60


@dataclass
class Decision:
    ts: str
    equity: float
    stale: bool
    n_members: int
    ic_est: float
    targets: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    risk: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    hold: list[str] = field(default_factory=list)  # positions left exactly as they are this bar


class LiveEngine:
    def __init__(
        self,
        cfg: HermesConfig,
        bundle: ModelBundle,
        feed: BinanceLiveFeed | None,
        broker: Broker,
        store: StateStore,
        mode: str,
        model_dir: str | Path | None = None,
        operator_cfg: HermesConfig | None = None,
        overrides: dict[str, object] | None = None,
    ):
        """``operator_cfg``/``overrides`` let a hot-reloaded bundle be merged exactly like the first one."""
        if mode not in ("paper", "demo", "live"):
            raise ValueError(mode)
        self.bundle = bundle
        self.feed = feed
        self.broker = broker
        self.store = store
        self.mode = mode
        self.operator_cfg = operator_cfg
        self.overrides = overrides or {}
        self._set_config(cfg)
        rs = store.get("risk_state")
        state = RiskState(**rs) if isinstance(rs, dict) else RiskState()
        if isinstance(rs, dict) and rs.get("day"):
            state.day = pd.Timestamp(rs["day"])
        self.overlay = RiskOverlay(cfg.risk, state)
        self.candidates: list[str] = list(store.get("candidates", []) or [])  # type: ignore[arg-type]
        self.candidates_day = store.get("candidates_day")
        self._venue: tuple[str, set[str]] | None = None  # (UTC day, OKX crypto swaps) for the paper broker
        self.model_dir = Path(model_dir) if model_dir is not None else None
        self._model_mtime = self._bundle_mtime()
        self._cache: dict[str, pd.DataFrame] = {}
        self.clock = lambda: pd.Timestamp.now(tz="UTC")  # replaced in tests and demonstrations
        # New-listing short sleeve (paper and demo only: real money needs an explicit decision and a promotion).
        sl = cfg.live.listing_sleeve
        self.sleeve = ListingSleeve(sl, store) if sl.enabled and mode != "live" else None
        self.last_cycle_s: float | None = None
        self._closed_now: set[str] = set()  # contracts closed this cycle before the rebalance (stops)

    def _set_config(self, cfg: HermesConfig) -> None:
        self.cfg = cfg
        self.bpd = cfg.bars_per_day
        self.constructor = PortfolioConstructor(cfg.portfolio, cfg.bars_per_year, cfg.portfolio.ic_ref)
        # A 1/N book (portfolio.books): one constructor per sub-book, each with its own horizon and cost aversion.
        self.book_settings = [
            cfg.portfolio.model_copy(update={"holding_horizon": b.holding_horizon, "cost_aversion": b.cost_aversion})
            for b in cfg.portfolio.books
        ]
        self.book_constructors = [
            PortfolioConstructor(p, cfg.bars_per_year, cfg.portfolio.ic_ref) for p in self.book_settings
        ]

    def _bundle_mtime(self) -> float:
        if self.model_dir is None or not (self.model_dir / "bundle.json").exists():
            return 0.0
        return (self.model_dir / "bundle.json").stat().st_mtime

    def maybe_reload_bundle(self) -> bool:
        """Hot-swap the champion when the retraining job installed a new one (hashes are verified).

        The new bundle brings its own strategy configuration (features, labels, portfolio, costs), merged with
        the operator's settings exactly as at start-up. A bundle on another timeframe or data layout needs a
        different feed: the process exits so that the service manager restarts it on the new champion.
        """
        m = self._bundle_mtime()
        if not m or m == self._model_mtime or self.model_dir is None:
            return False
        try:
            new = ModelBundle.load(self.model_dir)
        except (OSError, ValueError) as exc:
            self.store.event("ERROR", f"nouveau modèle refusé : {exc}")
            self._model_mtime = m
            return False
        if self.mode == "live" and not new.promoted and not self.cfg.live.allow_unpromoted:
            # Same strategy? Identities recomputed by this code (a recorded hash depends on the code that trained).
            from hermes.research.run import config_hash

            if config_hash(new.config) != config_hash(self.bundle.config):
                self.store.event("WARNING", "nouveau modèle non promu : le modèle actuel reste en service en réel")
                self._model_mtime = m
                return False
            # Same strategy re-evaluated on more data and now failing the gate: a demotion. The engine takes
            # it, and decide() then flattens the book (live trading refuses an unpromoted bundle).
            self.store.event("ERROR", "modèle rétrogradé par sa dernière évaluation : le livre réel sera fermé")
        operator = self.operator_cfg or self.cfg
        new_cfg = live_config(new.config, operator, self.overrides)
        old = self.cfg.data
        if (new_cfg.data.bar, new_cfg.data.intrabar) != (old.bar, old.intrabar) or live_history_bars(
            new_cfg
        ) > live_history_bars(self.cfg):
            self.store.event("WARNING", "le nouveau modèle demande un autre flux de données : redémarrage du moteur")
            raise SystemExit(3)
        self.bundle, self._model_mtime = new, m
        self._set_config(new_cfg)
        set_pos = getattr(self.feed, "set_positioning", None)
        if set_pos is not None:  # a champion that reads other positioning data needs them from the next cycle
            set_pos(new_cfg.features.positioning if new_cfg.data.include_metrics else ())
        self.overlay.cfg = new_cfg.risk
        self.store.event(
            "INFO", f"modèle rechargé ({new.meta.get('config_hash')}, entraîné jusqu'au {new.meta.get('train_end')})"
        )
        return True

    # -- helpers --------------------------------------------------------------------------------------------
    def _save_risk_state(self) -> None:
        s = asdict(self.overlay.state)
        s["day"] = str(self.overlay.state.day) if self.overlay.state.day is not None else None
        s["events"] = s["events"][-50:]
        self.store.put("risk_state", s)

    def strategy_nav(self, equity: float) -> float:
        """Net asset value of the strategy's own capital (``capital_fraction`` of the account).

        The book trades ``frac * equity``, so a bar's strategy return is the account return divided by
        ``frac``; drawdown and daily-loss limits apply to this NAV, not to the (diluted) account. Transfers in or
        out of the account show up as returns: ``hermes live resume`` restarts the NAV after moving funds.
        When the fraction changes (or on the first run after an upgrade), the NAV restarts at ``frac * equity``
        and the risk state is rescaled to it, so the current drawdown is preserved rather than invented.
        """
        frac = min(float(self.cfg.live.capital_fraction), 1.0)
        if equity <= 0:
            return equity
        st = self.store.get("nav_state")
        valid = isinstance(st, dict) and float(st.get("equity", 0.0) or 0.0) > 0 and float(st.get("nav", 0.0)) > 0
        if valid and st.get("frac", frac) == frac:  # type: ignore[union-attr]
            r = (equity / float(st["equity"]) - 1.0) / frac  # type: ignore[index]
            nav = float(st["nav"]) * max(1.0 + r, 1e-6)  # type: ignore[index]
        else:
            nav = equity * frac
            # The overlay tracked either the previous NAV or, before NAV tracking existed, the account.
            prev = float(st["nav"]) * equity / float(st["equity"]) if valid else equity  # type: ignore[index]
            self._rescale_risk_state(nav / prev if prev > 0 else 1.0)
        self.store.put("nav_state", {"nav": nav, "equity": equity, "frac": frac})
        return nav

    def _rescale_risk_state(self, ratio: float) -> None:
        st = self.overlay.state
        st.peak_equity *= ratio
        st.day_start_equity *= ratio
        st.last_equity *= ratio
        self._save_risk_state()

    def tradable(self, symbols: list[str]) -> list[str]:
        u = self.cfg.data.universe
        out = [s for s in symbols if not is_excluded(s, u)]
        register = getattr(self.broker, "register", None)
        if register is not None:  # OKX: only contracts listed there (mapped on the fly)
            out = register(out)
        elif u.venue == "okx":  # paper: the same universe as research and as the OKX broker
            listed = self.okx_swaps()
            out = [s for s in out if okx_inst_id(s) in listed]
        return out

    def okx_swaps(self) -> set[str]:
        """OKX crypto USDT swaps live today (instrument list cached per day; snapshot if OKX is unreachable)."""
        today = self.clock().strftime("%Y-%m-%d")
        if self._venue is None or self._venue[0] != today:
            cat = OkxListing(Path(self.cfg.live.state_dir)).catalog()
            self._venue = (today, {inst for inst, (category, _, live) in cat.items() if category == "1" and live})
        return self._venue[1]

    def n_candidates(self) -> int:
        """Contracts watched each day: a superset of the point-in-time top-N (``live.candidates`` caps it)."""
        return min(self.cfg.live.candidates, 2 * self.cfg.data.universe.top_n + 10)

    async def refresh_candidates(self, held: list[str]) -> list[str]:
        """Today's candidates (cached per UTC day) plus anything held. Every symbol the engine may trade is
        (re-)registered with the broker on each call: after a restart the broker's contract map starts empty."""
        today = self.clock().strftime("%Y-%m-%d")
        if self.feed is not None and (self.candidates_day != today or not self.candidates):
            n = self.n_candidates()
            top = await self.feed.top_symbols(n * 2)
            self.candidates = self.tradable(top)[:n]
            self.candidates_day = today
            self.store.put("candidates", self.candidates)
            self.store.put("candidates_day", today)
        traded = list(self.store.get("traded_symbols", []) or [])  # type: ignore[arg-type]
        self.candidates = self.tradable(self.candidates)
        self.tradable(traded)  # model symbols of earlier positions (e.g. 1000PEPEUSDT for PEPE-USDT-SWAP)
        return sorted(set(self.candidates) | set(held))

    def _remember_traded(self, symbols: list[str]) -> None:
        traded = set(self.store.get("traded_symbols", []) or [])  # type: ignore[arg-type]
        if not set(symbols) <= traded:
            self.store.put("traded_symbols", sorted(traded | set(symbols)))

    def _check_drift(self, feats: FeatureSet, mask: pd.DataFrame, t: int, d: Decision) -> None:
        """Feature drift against the training profile of the bundle (models/drift.py): the member rows of the
        last day for contract-level features, one row a bar over the last week for market-level ones (shared by
        every member), the windows the thresholds were calibrated on. Recomputed from the warmed-up panel each
        bar, so a restart, a gap in the cycles or a new bundle needs no memory."""
        profile = self.bundle.meta.get("feature_profile")
        if not isinstance(profile, dict) or not profile:
            return
        warm = feature_warmup_bars(self.cfg)
        names = feats.names
        week = self.cfg.days(DRIFT_MARKET_DAYS)
        design = self.bundle.meta.get("drift_windows")
        same = isinstance(design, dict) and (design.get("contract_bars"), design.get("market_bars")) == (
            self.bpd,
            week,
        )
        blocks: list[tuple[np.ndarray, list[str]]] = []
        if t + 1 - self.bpd >= warm:
            X, _ = feats.stack(mask, rows=np.arange(t + 1 - self.bpd, t + 1), dtype=STORAGE_DTYPE)
            cols = [j for j, k in enumerate(names) if k not in feats.market]
            blocks.append((X[:, cols].astype(np.float32), [names[j] for j in cols]))
        market = list(feats.market)
        d.risk["psi_market"] = float(bool(market) and t + 1 - week >= warm)
        d.risk["psi_market_calibrated"] = float(same and bool(design.get("market")))  # type: ignore[union-attr]
        if d.risk["psi_market"]:
            rows = np.arange(t + 1 - week, t + 1)
            Xm = np.column_stack([feats.market[k].iloc[rows].to_numpy(STORAGE_DTYPE) for k in market])
            blocks.append((Xm.astype(np.float32), market))
        risk, notes = drift_report(profile, blocks, calibrated=same)
        d.risk.update(risk)
        d.notes.extend(notes)

    # -- score / realised-IC memory: in RAM, backed by the state store for restarts ----------------------------
    def _memory(self, name: str, ts: pd.Timestamp) -> pd.DataFrame:
        """Recent scores (bar x symbol): the base-bar window (realised IC) or 30 days (signal persistence)."""
        since = ts - pd.Timedelta(days=max(31.0, live_history_bars(self.cfg) / self.bpd + 1.0))
        cache = self._cache.get(name)
        if cache is None:
            cache = self.store.score_history(since).drop(columns="__MKT__", errors="ignore")
            if cache.empty:
                cache = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
        cache = cache[cache.index >= since]
        self._cache[name] = cache
        return cache

    def _remember(self, name: str, ts: pd.Timestamp, row: pd.Series) -> None:
        cache = self._memory(name, ts)
        new = pd.DataFrame([row.to_numpy()], index=pd.DatetimeIndex([ts]), columns=row.index)
        merged = pd.concat([cache[cache.index != ts], new]) if len(cache) else new
        self._cache[name] = merged.sort_index()

    def _series(self, name: str, ts: pd.Timestamp) -> pd.Series:
        since = ts - pd.Timedelta(days=SCORE_MEMORY_DAYS)
        cache = self._cache.get(name)
        if cache is None:
            stored = self.store.get_series(name, since)
            if stored.empty:
                stored = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
            cache = stored.rename(name).to_frame()
        cache = cache[cache.index >= since]
        self._cache[name] = cache
        return cache[name]

    def _record_once(self, name: str, values: pd.Series, ts: pd.Timestamp) -> None:
        """Persist the values of ``name`` not recorded yet (a value, once its horizon has elapsed, is final)."""
        new = values.dropna()
        new = new[~new.index.isin(self._series(name, ts).index)]
        if len(new):
            self.store.put_series(name, new)
            self._remember_series(name, new, ts)

    def _incubation_checks(
        self,
        mem: pd.DataFrame,
        panel: Panel,
        tgt: pd.DataFrame,
        H: int,
        ts: pd.Timestamp,
        members: list[str],
        daily: DailyHistory | None,
    ) -> None:
        """Pre-registered incubation checks, logged and never used for sizing (docs/RESULTS.md, § 10).

        * ``ic_raw``: rank IC of the raw score -- does the model still rank?
        * ``ic_lag``: rank IC of the score one holding horizon old -- does the slow part the book trades hold?
        * daily regime: ``btc_dd90`` (BTC close against its 90-day high), ``mkt_ret30`` (members' mean 30-day
          return), ``xs_ac1`` (trailing 30-day mean of the cross-sectional correlation of consecutive daily
          returns). Each explanation of the 2026 decay predicts a different pattern in them."""
        if len(mem):
            raw = mem.reindex(index=panel.index, columns=panel.symbols)
            self._record_once("ic_raw", _rank_ic(raw, tgt), ts)
            self._record_once("ic_lag", _rank_ic(raw.shift(H), tgt), ts)
        if daily is None or daily.close.empty:
            return
        close = daily.close.sort_index()
        day = close.index[-1]
        if day in self._series("btc_dd90", ts).index:
            return
        out: dict[str, float] = {}
        if "BTCUSDT" in close:
            btc = close["BTCUSDT"].dropna().iloc[-90:]
            if len(btc) >= 30:
                out["btc_dd90"] = float(btc.iloc[-1] / btc.max() - 1.0)
        mc = close.reindex(columns=[s for s in members if s in close.columns])
        if mc.shape[1] >= 5 and len(mc) > 31:
            out["mkt_ret30"] = float((mc.iloc[-1] / mc.iloc[-31] - 1.0).mean())
            r = mc.pct_change().iloc[-31:]
            ac = rowwise_corr(r.iloc[1:], r.shift(1).iloc[1:])
            out["xs_ac1"] = float(ac.mean()) if ac.notna().any() else float("nan")
        for name, value in out.items():
            self._record_once(name, pd.Series({day: value}), ts)

    def _remember_series(self, name: str, values: pd.Series, ts: pd.Timestamp) -> None:
        cur = self._series(name, ts)
        merged = pd.concat([cur[~cur.index.isin(values.index)], values.astype(float)]).sort_index()
        self._cache[name] = merged.to_frame(name)

    def _horizon_signal(
        self,
        mem: pd.DataFrame,
        panel: Panel,
        tgt: pd.DataFrame,
        H: int,
        ts: pd.Timestamp,
        grid: pd.DatetimeIndex,
        gate: float,
    ) -> tuple[pd.Series, float, float]:
        """Traded score, IC estimate and cost amortisation of a sub-book horizon (realised IC kept as ``ic_h{H}``)."""
        cfg = self.cfg
        freq = BAR_TO_OFFSET[panel.bar]
        names = smooth_scores(mem, cfg.portfolio.signal_halflife * H)
        traded = names.iloc[-1] if len(names) and names.index[-1] == ts else pd.Series(dtype=float)
        key = f"ic_h{H}"
        ric_new = rowwise_corr(names.reindex(index=panel.index, columns=panel.symbols), tgt).dropna()
        ric_new = ric_new[~ric_new.index.isin(self._series(key, ts).index)]
        self.store.put_series(key, ric_new)
        self._remember_series(key, ric_new, ts)
        est = estimate_ic(self._series(key, ts).reindex(grid), H, self.bundle.prior_ic, halflife_bars=self.bpd * 30)
        ic_est = (float(est.iloc[-1]) if np.isfinite(est.iloc[-1]) else self.bundle.prior_ic) * gate
        cost_scale = float(self.bundle.meta.get("cost_scale", 1.0) or 1.0)  # type: ignore[arg-type]
        window = self.bpd * 30
        recent = names.reindex(pd.date_range(end=ts, periods=window + H, freq=freq))
        if recent.notna().any(axis=1).sum() > 4 * H:
            cost_scale = float(signal_persistence(recent, H, window, floor=cfg.portfolio.cost_scale_floor).iloc[-1])
        return traded, ic_est, cost_scale

    def _book_state(self, syms: list[str], pos_w: np.ndarray, close: np.ndarray, capital: float, K: int) -> np.ndarray:
        """Sub-book positions (fractions of the capital) as saved at the last decision, drifted with prices like
        the backtest (x price ratio / capital ratio), then reconciled with the real positions: a contract that
        is flat (closed, stopped) is flat in every sub-book, a partial fill or an outside change rescales them,
        and anything the sub-books cannot explain is split equally."""
        st = self.store.get("book_state")
        sub = np.zeros((K, len(syms)))
        settings = [[p.holding_horizon, p.cost_aversion] for p in self.book_settings]
        if isinstance(st, dict) and st.get("settings") == settings and float(st.get("capital", 0) or 0) > 0:
            ratio_cap = float(st["capital"]) / max(capital, 1e-9)
            for i, s in enumerate(syms):
                w_then = st.get("w", {}).get(s)
                px_then = st.get("close", {}).get(s)
                if w_then is None or not px_then or not np.isfinite(close[i]):
                    continue
                sub[:, i] = np.asarray(w_then, float) * (close[i] / float(px_then)) * ratio_cap
        tot = sub.sum(axis=0)
        for i in range(len(syms)):
            if pos_w[i] == 0:
                sub[:, i] = 0.0
            elif tot[i] != 0 and np.sign(tot[i]) == np.sign(pos_w[i]):
                sub[:, i] *= pos_w[i] / tot[i]
            else:
                sub[:, i] = pos_w[i] / K
        return sub

    def _save_book_state(self, sub: np.ndarray, syms: list[str], close: np.ndarray, capital: float) -> None:
        held = np.nonzero(np.any(sub != 0, axis=0) & np.isfinite(close))[0]
        self.store.put(
            "book_state",
            {
                "settings": [[p.holding_horizon, p.cost_aversion] for p in self.book_settings],
                "capital": capital,
                "w": {syms[i]: [round(float(x), 10) for x in sub[:, i]] for i in held},
                "close": {syms[i]: float(close[i]) for i in held},
            },
        )

    def _holding_horizon(self, horizons: dict[int, object], H: int | None = None) -> int:
        """The configured holding horizon (or ``H``), or the nearest one the targets carry (as in research)."""
        H = self.cfg.portfolio.holding_horizon if H is None else H
        return H if H in horizons else min(horizons, key=lambda h: abs(h - H))

    # -- decision (pure given inputs; unit-testable offline) ----------------------------------------------
    def decide(
        self,
        panel: Panel,
        positions_notional: dict[str, float],
        equity: float,
        now: pd.Timestamp | None = None,
        daily: DailyHistory | None = None,
        nav: float | None = None,
    ) -> Decision:
        """Target notionals for the next bar.

        ``equity`` is the account equity; the book is sized on ``live.capital_fraction`` of it, and the risk
        overlay follows ``nav`` (the strategy's own NAV, see ``strategy_nav``; defaults to the capital).
        """
        cfg = self.cfg
        t = len(panel.index) - 1
        ts = panel.index[t]
        expected = BinanceLiveFeed.last_closed_bar(panel.bar, now) if now is not None else ts
        stale = ts < expected
        capital = equity * cfg.live.capital_fraction
        nav = capital if nav is None else nav
        d = Decision(ts=str(ts), equity=equity, stale=bool(stale), n_members=0, ic_est=0.0)
        # The universe needs months of daily history (liquidity, listing age); the base-bar window is short.
        if daily is not None:
            mask = universe_mask(panel, cfg.data.universe, daily_qv=daily.quote_volume, daily_alive=daily.alive)
        else:
            mask = universe_mask(panel, cfg.data.universe)
        feats = build_features(panel, mask, cfg.features)
        if feats.names != self.bundle.feature_names:
            missing = set(self.bundle.feature_names) - set(feats.names)
            raise RuntimeError(f"feature mismatch with the bundle (missing {sorted(missing)[:5]}...)")
        # Rounded like the stored training rows (research/dataset.py): the models score the values they were fit on.
        X, mi = feats.stack(mask, rows=np.array([t]), dtype=STORAGE_DTYPE)
        X = X.astype(np.float32)
        members = list(mi.get_level_values(1))
        d.n_members = len(members)
        try:  # a monitoring failure must never stop the book from trading
            self._check_drift(feats, mask, t, d)
        except Exception:
            log.exception("drift check failed")
            d.risk["psi_error"] = 1.0
        scores = pd.Series(self.bundle.score(X, np.zeros(len(X), dtype=np.int64)), index=members, dtype=float)
        d.scores = {k: round(float(v), 4) for k, v in scores.items() if np.isfinite(v)}
        self.store.add_scores(ts, scores)
        self._remember("scores", ts, scores)
        use_market = bool(self.bundle.meta.get("market_promoted")) and self.bundle.market is not None
        if use_market:
            x = np.array([float(feats.market[k].iloc[t]) for k in self.bundle.market_feature_names])
            m_score = pd.Series({ts: self.bundle.market_score(x)})
            self.store.put_series("mkt_score", m_score)
            self._remember_series("mkt_score", m_score, ts)

        # Realised IC of the live scores -> causal IC estimate (prior = bundle's validation lower bound).
        # Realised values are persisted once their horizon has elapsed, so the estimate reads months of
        # history (as in research) even though the base-bar window holds only a few weeks.
        targets = build_targets(panel, feats, mask, cfg.labels)
        H = self._holding_horizon(targets.residual)
        tgt = targets.residual[H]
        freq = BAR_TO_OFFSET[panel.bar]
        grid = pd.date_range(end=ts, periods=SCORE_MEMORY_DAYS * self.bpd, freq=freq)
        # The book trades the smoothed score (portfolio.signal_halflife), exactly as in research: its realised
        # IC and persistence are measured on that same series.
        mem = self._memory("scores", ts)
        if len(mem):  # regular bar grid: skipped bars decay the average exactly as in research
            mem = mem.reindex(pd.date_range(mem.index[0], ts, freq=freq))
        names = smooth_scores(mem, cfg.portfolio.signal_halflife * H)
        traded = names.iloc[-1] if len(names) and names.index[-1] == ts else pd.Series(dtype=float)
        ric_new = rowwise_corr(names.reindex(index=panel.index, columns=panel.symbols), tgt).dropna()
        # Each bar's realised IC is recorded once, when its horizon has just elapsed (the end of the window,
        # where features and targets are fully warmed up); older values are never rewritten from a window
        # that has slid past their warm-up.
        ric_new = ric_new[~ric_new.index.isin(self._series("ic", ts).index)]
        self.store.put_series("ic", ric_new)
        self._remember_series("ic", ric_new, ts)
        self._incubation_checks(mem, panel, tgt, H, ts, members, daily)
        ric = self._series("ic", ts).reindex(grid)
        est = estimate_ic(ric, H, self.bundle.prior_ic, halflife_bars=self.bpd * 30)
        ic_est = float(est.iloc[-1]) if np.isfinite(est.iloc[-1]) else self.bundle.prior_ic
        pc = cfg.portfolio
        g = 1.0
        if pc.regime_gate_drawdown > 0 and daily is not None and pc.regime_gate_symbol in daily.close:
            gate = regime_scale(
                daily.close[pc.regime_gate_symbol],
                pc.regime_gate_drawdown,
                pc.regime_gate_lookback_days,
                pc.regime_gate_scale,
            )
            g = float(gate.get(ts.floor("D"), 1.0))
            ic_est *= g
            d.risk["regime_scale"] = g
        cost_scale = float(self.bundle.meta.get("cost_scale", 1.0) or 1.0)  # type: ignore[arg-type]
        window = self.bpd * 30
        recent = names.reindex(pd.date_range(end=ts, periods=window + H, freq=freq))
        if recent.notna().any(axis=1).sum() > 4 * H:
            cost_scale = float(signal_persistence(recent, H, window, floor=cfg.portfolio.cost_scale_floor).iloc[-1])
        # Sub-books of a 1/N book on other horizons: the same smoothing, realised IC (own series), IC estimate
        # and cost amortisation at their horizon, as book_signal builds them in research.
        horizon_signals = {H: (traded, ic_est, cost_scale)}
        book_h = [self._holding_horizon(targets.residual, p_.holding_horizon) for p_ in self.book_settings]
        for Hj in book_h:
            if Hj not in horizon_signals:
                horizon_signals[Hj] = self._horizon_signal(mem, panel, targets.residual[Hj], Hj, ts, grid, g)
        if book_h:
            ic_est = float(np.mean([horizon_signals[h_][1] for h_ in book_h]))
            cost_scale = float(np.mean([horizon_signals[h_][2] for h_ in book_h]))
            for h_, (_, ic_h, _) in sorted(horizon_signals.items()):
                d.risk[f"ic_h{h_}"] = round(float(ic_h), 5)
        d.ic_est = round(ic_est, 5)
        d.risk["cost_scale"] = round(cost_scale, 3)
        # Paper incubation at nominal size (live.paper_nominal_size): the book is sized as if each horizon's IC
        # were at least research's out-of-sample IC, so that the signal is traded and measured even while the live
        # estimate is zero. Demo and live keep the IC-scaled sizing (a zero estimate keeps the real book flat).
        ic_size = ic_est
        if self.mode == "paper" and cfg.live.paper_nominal_size:
            rs = self.bundle.meta.get("research")
            rs = rs if isinstance(rs, dict) else {}
            by_h = rs.get("ic_by_horizon") if isinstance(rs.get("ic_by_horizon"), dict) else {}

            def nominal(h: int) -> float:
                v = by_h.get(str(h), rs.get("ic"))
                return float(v) if isinstance(v, (int, float)) and np.isfinite(v) and v > 0 else 0.0

            horizon_signals = {h_: (tr, max(ic_h, nominal(h_)), cs) for h_, (tr, ic_h, cs) in horizon_signals.items()}
            ic_size = float(np.mean([horizon_signals[h_][1] for h_ in (book_h or [H])]))
            if ic_size > ic_est:
                d.risk["ic_sizing"] = round(ic_size, 5)
                d.notes.append(
                    f"papier : taille nominale (IC de recherche {ic_size:.3f} ; IC estimé en direct {ic_est:.3f})"
                )
        m_alpha = 0.0
        if use_market:
            mt_new = targets.market[H].dropna()
            mt_new = mt_new[~mt_new.index.isin(self._series("mkt_target", ts).index)]
            self.store.put_series("mkt_target", mt_new)
            self._remember_series("mkt_target", mt_new, ts)
            ms = self._series("mkt_score", ts).reindex(grid)
            if ms.notna().sum() > 24:
                mt = self._series("mkt_target", ts).reindex(grid)
                mret = feats.aux["mkt"]["mkt"].reindex(grid)
                ma = market_alpha_series(ms, mt, mret, self.bundle.market_prior_ic, H, self.bpd)
                m_alpha = float(ma.iloc[-1]) if np.isfinite(ma.iloc[-1]) else 0.0
        d.risk["market_alpha"] = round(m_alpha, 6)

        # Book construction on members plus anything still held (weights are fractions of the capital).
        syms = panel.symbols
        pos_w = np.array([positions_notional.get(s, 0.0) / max(capital, 1e-9) for s in syms])
        close_t = panel["close"].iloc[t].to_numpy()
        # A held contract without a price for this bar, or without the daily history its universe membership
        # needs (a failed fetch), is left untouched: never liquidated blind on missing data.
        frozen = (pos_w != 0) & ~np.isfinite(close_t)
        if daily is not None:
            has_daily = daily.quote_volume.reindex(columns=syms).notna().any(axis=0).to_numpy()
            frozen |= (pos_w != 0) & ~has_daily
        active = mask.iloc[t].to_numpy() & np.isin(syms, members)
        idx = np.nonzero((active | (pos_w != 0)) & ~frozen)[0]

        # The traded (smoothed) score, re-standardised across this bar's members exactly as the backtest does
        # (cs_zscore over the universe, clipped at 3): smoothing shrinks the dispersion, sizing must not.
        def zvec(traded_: pd.Series) -> np.ndarray:
            row = pd.Series({s: traded_.get(s, np.nan) for s in scores.index}, dtype=float)
            row = row.where(np.isfinite(row), scores.reindex(row.index))
            frame = pd.DataFrame([row.to_numpy()], index=mask.index[[t]], columns=row.index)
            zrow = cs_zscore(frame, mask.iloc[[t]].reindex(columns=row.index)).iloc[0]
            out = np.full(len(syms), np.nan)
            for s, v in zrow.items():
                out[syms.index(s)] = v
            return out

        z = zvec(traded)
        costs = CostModel.from_panel(
            cfg.costs, panel["high"], panel["low"], panel["close"], panel["quote_volume"], feats.aux["vol"], self.bpd
        )
        r = (panel["close"] / panel["close"].shift(1) - 1).to_numpy()
        cov_hl = cfg.days(cfg.portfolio.cov_halflife_days)
        ewma = EwmaCovariance(len(syms), cov_hl)
        for k in range(max(0, t - cov_hl * 2), t + 1):
            ewma.update(np.where(mask.iloc[k].to_numpy(), r[k], np.nan))
        mvar = market_variance(feats.aux["mkt"]["mkt"], max(2, cov_hl // 4)).iloc[t]
        dollars_typ = np.abs(pos_w[idx]).mean() * capital + 1.0 if len(idx) else 1.0
        inp = BookInputs(
            score=np.where(active[idx], z[idx], np.nan),
            ivol=feats.aux["ivol"].iloc[t].to_numpy()[idx],
            beta=feats.aux["beta"].iloc[t].to_numpy()[idx],
            mkt_var=float(mvar) if np.isfinite(mvar) else 1e-4,
            cost_rate=costs.linear_rate(t, dollars_typ)[idx] * cost_scale,
            adv=costs.adv.iloc[t].to_numpy()[idx],
            w0=pos_w[idx],
            ic=ic_size,
            market_alpha=m_alpha,
            sample_cov=ewma.matrix(idx),
        )
        props = None
        if not self.book_settings:
            book = self.constructor.target(inp, capital)
            target = book.weights
            ex_ante = book.ex_ante_vol_annual
        else:
            K = len(self.book_settings)
            sub = self._book_state(syms, pos_w, close_t, capital, K)
            props = np.zeros((K, len(idx)))
            lin = costs.linear_rate(t, dollars_typ)[idx]
            for j, (Hj, cons) in enumerate(zip(book_h, self.book_constructors, strict=True)):
                tr_j, ic_j, cs_j = horizon_signals[Hj]
                z_j = z if Hj == H else zvec(tr_j)
                inp_j = replace(
                    inp,
                    score=np.where(active[idx], z_j[idx], np.nan),
                    cost_rate=lin * cs_j,
                    w0=K * sub[j, idx],  # each sub-book on its own capital share, as in the backtest
                    ic=ic_j,
                )
                book = cons.target(inp_j, capital)
                props[j] = book.weights / K
            target = props.sum(axis=0)
            ex_ante = float(np.sqrt(max(target @ book.cov_bar @ target, 0.0) * cfg.bars_per_year))
            d.risk["books"] = float(K)
        proposal = target.copy()
        if stale:
            target = RiskOverlay.restrict_to_reductions(target, pos_w[idx])
            d.notes.append("données périmées : réductions seulement")
        if self.mode == "live" and not self.bundle.promoted and not cfg.live.allow_unpromoted:
            target = np.zeros_like(target)
            d.notes.append("modèle non promu : trading réel refusé, livre fermé")
        self.overlay.observe(ts, nav)
        w, info = self.overlay.apply(
            ts, nav, target, pos_w[idx], book.cov_bar, self.bpd, self._daily_returns(panel, daily, ts, idx)
        )
        d.risk.update({k: round(float(v), 5) for k, v in info.items()})
        d.risk["drawdown"] = round(self.overlay.drawdown(nav), 5)
        d.risk["halted"] = float(self.overlay.state.halted)
        d.risk["ex_ante_vol"] = round(ex_ante, 4)
        # Risk of the book actually held after the overlay (what the expected-equity cone integrates).
        d.risk["ex_ante_vol_held"] = round(float(np.sqrt(max(w @ book.cov_bar @ w, 0.0) * cfg.bars_per_year)), 5)
        if props is not None:
            # The overlay scales or drops positions of the sum: each sub-book follows its contract's ratio.
            safe = np.where(proposal != 0, proposal, 1.0)
            sub[:, idx] = props * np.where(proposal != 0, w / safe, 1.0)
            self._save_book_state(sub, syms, close_t, capital)
        d.risk["nav"] = round(nav, 2)
        for j, i in enumerate(idx):
            if w[j] != 0 or pos_w[i] != 0:
                d.weights[syms[i]] = round(float(w[j]), 5)
                d.targets[syms[i]] = float(w[j] * capital)
        for i in np.nonzero(frozen)[0]:
            d.hold.append(syms[i])
            d.notes.append(f"{syms[i]} : données manquantes à cette bougie, position laissée telle quelle")
        for s, v in positions_notional.items():
            if s not in syms and v != 0:
                d.hold.append(s)
                d.notes.append(f"{s} : absent du flux de données, position laissée telle quelle")
        self._save_risk_state()
        return d

    @staticmethod
    def _daily_returns(panel: Panel, daily: DailyHistory | None, ts: pd.Timestamp, idx: np.ndarray) -> np.ndarray:
        """Up to 180 closed daily returns of the book's contracts for the historical ES (as in the backtest)."""
        dc = daily.close.reindex(columns=panel.symbols) if daily is not None else panel["close"].resample("1D").last()
        dc = dc[dc.index < ts.floor("D")]
        ret = (dc / dc.shift(1) - 1.0).iloc[1:].iloc[-180:].to_numpy()
        return np.nan_to_num(ret[:, idx]) if len(ret) else np.zeros((0, len(idx)))

    # -- one cycle --------------------------------------------------------------------------------------------
    async def _flatten(self, reason: str) -> None:
        """Close everything at once (kill switch, hard drawdown halt); works without market data."""
        positions = await self.broker.positions()
        if self.sleeve is not None and self.sleeve.open:
            conv = getattr(self.broker, "price_to_model", None) or (lambda _s, px: px)
            marks = {s: float(conv(s, p.mark_px)) for s, p in positions.items() if p.mark_px}
            self.sleeve.close_all(marks, self.clock(), "halt")
        if positions:
            rep = await self.broker.rebalance({}, urgent=True)
            self.store.add_fills(rep.fills, "flatten", getattr(self.broker, "price_to_model", None))
            for e in rep.errors:
                self.store.event("ERROR", e)
            await self.broker.protect({})
        equity = await self.broker.equity()
        left = await self.broker.positions()
        prev = self._read_status()
        self.store.write_status(
            {
                **{k: prev[k] for k in ("bar", "capital_fraction", "ic_est", "execution", "n_members") if k in prev},
                "mode": self.mode,
                "updated": self.clock().isoformat(),
                "halted": True,
                "halt_reason": reason,
                "equity": equity,
                "gross": 0.0 if not left else prev.get("gross"),
                "net": 0.0 if not left else prev.get("net"),
                "risk": {
                    **(prev.get("risk") or {}),
                    "halted": 1.0,
                    "drawdown": round(self.overlay.drawdown(self.strategy_nav(equity)), 5),
                },
                "notes": [f"ARRÊT : {reason}"],
                "positions": {s: round(p.notional, 2) for s, p in left.items()},
                "positions_detail": await self._positions_detail(
                    left,
                    Decision("", equity, False, 0, 0.0),
                    max(equity * self.cfg.live.capital_fraction, 1e-9),
                    self.clock(),
                )
                if left
                else [],
                "bundle": self._bundle_summary(),
                "risk_limits": self._risk_limits(),
                "account": self._account(equity),
                "strategy": self._strategy_summary(),
                "listing_sleeve": self.sleeve.summary({}) if self.sleeve is not None else {"enabled": False},
            }
        )

    async def guard(self) -> bool:
        """Risk checks that must not depend on the market-data feed or the model: kill switch and the
        drawdown halt. Returns True (after flattening) when the engine must not trade this bar."""
        ts = BinanceLiveFeed.last_closed_bar(self.cfg.data.bar, self.clock())
        if self.overlay.kill_requested() and not self.overlay.state.halted:
            self.overlay.halt(ts, "interrupteur d'urgence posé")
        if not self.overlay.state.halted:
            nav = self.strategy_nav(await self.broker.equity())
            self.overlay.observe(ts, nav)
        if self.overlay.state.halted:
            self._save_risk_state()
            await self._flatten(self.overlay.state.halt_reason)
            await alert(f"ARRÊT : {self.overlay.state.halt_reason}", "ERROR")
            return True
        return False

    async def step(self) -> Decision | None:
        assert self.feed is not None
        self.maybe_reload_bundle()
        if await self.guard():
            return None
        every = self.cfg.portfolio.rebalance_every
        decision_bar = True
        if every > 1:
            last = BinanceLiveFeed.last_closed_bar(self.cfg.data.bar, self.clock())
            bar = pd.Timedelta(BAR_TO_OFFSET[self.cfg.data.bar])
            decision_bar = bool(is_rebalance_bar(pd.DatetimeIndex([last]), bar, every)[0])
        positions = await self.broker.positions()
        self._reconcile_external(positions)
        extra: list[str] = []
        if self.sleeve is not None:
            try:  # the sleeve never blocks the book: a short budget, and a failed refresh is retried in an hour
                await asyncio.wait_for(self.sleeve.refresh(self.feed, self.clock()), timeout=20.0)
            except Exception:
                log.exception("listing sleeve: calendar refresh failed")
                self.sleeve.backoff(self.clock())
            extra = self.tradable(self.sleeve.symbols_needed(self.clock()))
        symbols = await self.refresh_candidates(list(positions) + extra)
        # Market data must arrive within the bar; otherwise the cycle fails and the guard runs again.
        budget = 0.6 * self.cfg.bar_minutes * 60.0
        panel = await asyncio.wait_for(self.feed.update(symbols), timeout=budget)
        daily = await asyncio.wait_for(self.feed.daily(symbols), timeout=budget)
        now = self.clock()
        stop_fills: dict[str, float | None] = {}
        if isinstance(self.broker, PaperBroker):
            last = {f: panel[f].iloc[-1].dropna().to_dict() for f in ("open", "high", "low", "close")}
            stopped = self.broker.check_stops(last["high"], last["low"], last["open"])  # before the new marks
            stop_fills = {f.symbol: f.price for f in stopped}
            if stopped:
                self.store.add_fills(stopped, "stop")
                self._closed_now.update(f.symbol for f in stopped)
                self.store.event("WARNING", f"stops déclenchés (papier) : {', '.join(f.symbol for f in stopped)}")
            self.broker.set_prices(last["close"])
            self._accrue_paper_funding(panel)
        equity = await self.broker.equity()
        positions = await self.broker.positions()
        notional = {s: p.notional for s, p in positions.items()}
        prices = {s: float(v) for s, v in panel["close"].iloc[-1].dropna().items()}
        if self.sleeve is not None:
            # The book is decided on the account net of the sleeve's legs; the two target lists are added below.
            self.sleeve.set_nav(self.strategy_nav(equity))
            self._sleeve_funding(panel, prices)
            hedge_pre = self.sleeve.holdings(prices).get(HEDGE, 0.0)  # still on the account until this rebalance
            self.sleeve.on_stops({**{s: None for s in self._closed_now}, **stop_fills}, prices, now)
            self.sleeve.reconcile(notional, prices, now)
            held = self.sleeve.holdings(prices)
            # A released hedge stays on the account until the summed targets unwind it; a stopped BTC position
            # took the hedge with it.
            held[HEDGE] = 0.0 if HEDGE in self._closed_now else hedge_pre
            notional = {s: notional.get(s, 0.0) - held.get(s, 0.0) for s in set(notional) | set(held)}
            notional = {s: v for s, v in notional.items() if abs(v) >= 1.0}
        d = self.decide(panel, notional, equity, now=now, daily=daily, nav=self.strategy_nav(equity))
        if not decision_bar:
            # Scores, realised IC and the smoothed signal are updated every bar, as in research; the book only
            # moves on the clock-aligned decision grid (portfolio.rebalance_every).
            d.notes.append("bougie de notation : pas d'échange (portfolio.rebalance_every)")
            return d
        if d.stale:
            await alert(f"données périmées ({d.ts}) : aucune nouvelle prise de risque", "WARNING")
        urgent = self.overlay.state.halted
        targets = dict(d.targets)
        if self.sleeve is not None:
            if urgent:  # the halt found in this decision closes the sleeve's legs in the same urgent rebalance
                self.sleeve.close_all(prices, now, "halt")
            for s_, v_ in self._sleeve_targets(panel, prices, now, float(d.risk.get("nav", equity)), d).items():
                targets[s_] = targets.get(s_, 0.0) + v_
        self._remember_traded([s for s, v in targets.items() if v != 0])
        rep = await self.broker.rebalance(targets, urgent=urgent, hold=set(d.hold))
        self.store.add_fills(rep.fills, "flatten" if urgent else "trade", getattr(self.broker, "price_to_model", None))
        for e in rep.errors:
            self.store.event("ERROR", e)
        shortfall = self._shortfall_bps(rep.fills, panel["close"].iloc[-1])
        if np.isfinite(shortfall):
            self.store.put_series("shortfall_bps", pd.Series({pd.Timestamp(d.ts): shortfall}))
        # Exchange-side catastrophe stops, k daily sigmas away (same EWMA half-life as the features).
        hl = self.cfg.bars(self.cfg.features.vol_halflife_minutes)
        vol = panel["close"].pipe(np.log).diff().ewm(halflife=hl, adjust=False).std()
        sig_d = (vol.iloc[-1] * np.sqrt(self.bpd)).fillna(0.05)
        stops = {
            s: float(np.clip(self.cfg.risk.stop_loss_daily_sigmas * sig_d.get(s, 0.05), 0.03, 0.5))
            for s, v in targets.items()
            if v != 0
        }
        if self.sleeve is not None:  # the sleeve's own stop (or none) on its shorts, not the book's
            for s_ in self.sleeve.coins():
                stops.pop(s_, None)
            stops.update(self.sleeve.stop_fractions(prices))
            if not d.targets.get(HEDGE):  # BTC held only as the hedge: no stop on half of a hedged pair
                stops.pop(HEDGE, None)
        await self.broker.protect(stops)
        equity_after = await self.broker.equity()
        pos_after = await self.broker.positions()
        capital_after = max(equity_after * self.cfg.live.capital_fraction, 1e-9)
        gross = sum(abs(p.notional) for p in pos_after.values()) / capital_after
        net = sum(p.notional for p in pos_after.values()) / capital_after
        ts = pd.Timestamp(d.ts)
        self.store.add_decision(ts, asdict(d))
        nav_after = float(d.risk.get("nav", equity_after))
        vol_held = float(d.risk.get("ex_ante_vol_held", 0.0)) if pos_after else 0.0
        self.store.add_equity(
            ts, equity_after, gross, net, self.overlay.drawdown(nav_after), d.ic_est, len(pos_after), vol_held
        )
        if ts.minute == 0 and ts.hour == 0:
            self.store.prune(ts - pd.Timedelta(days=SCORE_MEMORY_DAYS + 10))
        status = {
            "mode": self.mode,
            "updated": now.isoformat(),
            "bar": d.ts,
            "equity": equity_after,
            "capital_fraction": self.cfg.live.capital_fraction,
            "gross": gross,
            "net": net,
            "positions": {s: round(p.notional, 2) for s, p in pos_after.items()},
            "ic_est": d.ic_est,
            "risk": d.risk,
            "notes": d.notes,
            "execution": {
                "traded": rep.traded_notional,
                "fees": rep.fees,
                "maker_share": rep.maker_share,
                "unfilled": rep.unfilled,
                "shortfall_bps": round(shortfall, 2) if np.isfinite(shortfall) else None,
                "errors": rep.errors[:10],
            },
            "bundle": self._bundle_summary(),
            "risk_limits": self._risk_limits(),
            "halted": self.overlay.state.halted,
            "halt_reason": self.overlay.state.halt_reason,
            "positions_detail": await self._positions_detail(pos_after, d, capital_after, now, targets),
            "account": self._account(equity_after),
            "strategy": self._strategy_summary(),
            "listing_sleeve": self.sleeve.summary(prices) if self.sleeve is not None else {"enabled": False},
            "n_members": d.n_members,
            "cycle_s": self.last_cycle_s,
        }
        self.store.write_status(status)
        if self.overlay.state.halted:
            await alert(f"ARRÊT : {self.overlay.state.halt_reason}", "ERROR")
        elif self.overlay.cushion_exhausted(nav_after) and self.store.get("cushion_alert_day") != str(ts.date()):
            self.store.put("cushion_alert_day", str(ts.date()))  # once a day, not every bar
            await alert(
                f"drawdown {self.overlay.drawdown(nav_after):.1%} : budget de risque < 5 %, livre quasi à l'arrêt "
                "(décision humaine : hermes live kill, ou reprise)",
                "WARNING",
            )
        log.info(
            "bar %s equity %.2f gross %.2f net %.2f ic %.4f traded %.0f maker %.0f%%",
            d.ts,
            equity_after,
            gross,
            net,
            d.ic_est,
            rep.traded_notional,
            100 * rep.maker_share,
        )
        return d

    def _sleeve_targets(
        self, panel: Panel, prices: dict[str, float], now: pd.Timestamp, nav: float, d: Decision
    ) -> dict[str, float]:
        """The listing sleeve's targets for this bar; no new entry on stale data or while halted, and any failure
        keeps its current legs rather than stopping the book."""
        assert self.sleeve is not None
        try:
            due = sorted({s for s, _ in self.sleeve.due(now)})
            close = panel["close"]
            vol: dict[str, float] = {}
            for s in due:
                if s not in close:
                    continue
                r = np.log(close[s].dropna()).diff().iloc[2:]  # skip the listing bar, as in research
                if len(r) >= max(2, self.bpd // 4):  # at least 6 hours of bars
                    vol[s] = float(r.std() * np.sqrt(self.bpd))
            # No new short while the book is restricted: stale data, a halt, the daily-loss breaker or any
            # drawdown de-risking (the overlay's limits bind the whole account, not only the book).
            r = d.risk
            entries = (
                not d.stale
                and not self.overlay.state.halted
                and not r.get("reduce_only")
                and float(r.get("budget", 1.0)) >= 0.999
            )
            return self.sleeve.targets(now, prices, nav, set(self.tradable(due)), vol, entries=entries)
        except Exception:
            log.exception("listing sleeve: targets failed, legs kept")
            return self.sleeve.holdings(prices)

    def _reconcile_external(self, positions: dict[str, Position]) -> None:
        """Record what changed on the exchange since the engine's last cycle without going through it (an
        exchange-side catastrophe stop, a liquidation, a manual trade), so the trade history stays whole. The
        paper broker records its own stops; on OKX the size change is known, its price only approximately (the
        stop's trigger when one was placed and the position is gone, else the current mark), fees unknown."""
        self._closed_now = set()
        if isinstance(self.broker, PaperBroker):
            return
        prev = self.store.get("book_after") or {}
        if not isinstance(prev, dict):
            return
        conv = getattr(self.broker, "price_to_model", None) or (lambda _s, px: px)
        fills = []
        for s, rec in prev.items():
            try:
                c0, n0, px0, stop = (float(x) if x is not None else None for x in rec)  # type: ignore[union-attr]
            except (TypeError, ValueError):
                continue
            if not c0:
                continue
            p = positions.get(s)
            c1 = p.contracts if p is not None else 0.0
            if abs(c1 - c0) <= 1e-9 * max(1.0, abs(c0)):
                continue
            gone = c1 == 0.0 or np.sign(c1) != np.sign(c0)
            px = stop if gone and stop else (float(conv(s, p.mark_px)) if p is not None else px0)
            if not px:
                continue
            dq = (0.0 if gone else c1) - c0  # the part closed outside the engine (a flip counts as a close)
            notional = abs(dq / c0) * abs(n0) * px / px0 if px0 else abs(n0)  # type: ignore[operator]
            kind = "stop" if gone and stop else "external"
            fills.append(Fill(s, "buy" if dq > 0 else "sell", dq, px, 0.0, False, notional=notional))
            self.store.event(
                "WARNING", f"{s} : position modifiée hors du moteur ({'stop' if kind == 'stop' else 'externe'})"
            )
            if gone:
                self._closed_now.add(s)
            self.store.add_fills(fills[-1:], kind)

    def _save_book(self, positions: dict[str, Position], stops: dict[str, float]) -> None:
        conv = getattr(self.broker, "price_to_model", None) or (lambda _s, px: px)
        self.store.put(
            "book_after",
            {s: [p.contracts, p.notional, float(conv(s, p.mark_px)), stops.get(s)] for s, p in positions.items()},
        )

    async def _positions_detail(
        self,
        positions: dict[str, Position],
        d: Decision,
        capital: float,
        now: pd.Timestamp,
        targets: dict[str, float] | None = None,
    ) -> list[dict[str, object]]:
        """Open positions as the dashboard shows them: prices in the model's (Binance) units, the catastrophe
        stop's trigger, unrealised P&L at mark, and when the position was opened (kept while its side is)."""
        try:
            stops = await self.broker.stop_levels()
        except Exception as exc:  # the positions are still worth showing
            log.warning("stop levels unavailable: %s", exc)
            stops = {}
        conv = getattr(self.broker, "price_to_model", None) or (lambda _s, px: px)
        self._save_book(positions, stops)
        prev = self.store.get("opened", {}) or {}
        sleeve_coins = self.sleeve.coins() if self.sleeve is not None else set()
        opened: dict[str, list[object]] = {}
        out = []
        for s, p in sorted(positions.items(), key=lambda kv: -abs(kv[1].notional)):
            if not p.notional:
                continue
            side = 1 if p.notional > 0 else -1
            # Closed earlier in this cycle (a stop) and reopened by the rebalance: a new position.
            old = prev.get(s) if isinstance(prev, dict) and s not in self._closed_now else None
            opened[s] = old if isinstance(old, list) and len(old) == 2 and old[0] == side else [side, now.isoformat()]
            entry, mark = float(conv(s, p.avg_px)), float(conv(s, p.mark_px))
            stop = stops.get(s)
            out.append(
                {
                    "symbol": s,
                    "side": "long" if side > 0 else "short",
                    "notional": round(p.notional, 2),
                    "weight": round(p.notional / capital, 5) if capital > 0 else None,
                    "entry": entry,
                    "mark": mark,
                    "upnl": round(p.notional * (1.0 - entry / mark), 2) if entry > 0 and mark > 0 else None,
                    "upnl_pct": round(side * (mark / entry - 1.0), 5) if entry > 0 and mark > 0 else None,
                    "stop": stop,
                    "stop_dist": round(side * (mark - stop) / mark, 5) if stop and mark > 0 else None,
                    "score": d.scores.get(s),
                    "target": round((targets if targets is not None else d.targets).get(s, 0.0), 2),
                    "sleeve": s in sleeve_coins,
                    "opened": opened[s][1],
                }
            )
        self.store.put("opened", opened)
        return out

    def _bundle_summary(self) -> dict[str, object]:
        keys = ("config_hash", "train_start", "train_end", "promoted", "research", "gate", "cost_scale")
        return {k: self.bundle.meta.get(k) for k in keys} | {"prior_ic": self.bundle.prior_ic}

    def _risk_limits(self) -> dict[str, float]:
        return {
            "drawdown_soft": self.cfg.risk.drawdown_soft,
            "drawdown_hard": self.cfg.risk.drawdown_hard,
            "daily_loss_limit": self.cfg.risk.daily_loss_limit,
            "gross_max": self.cfg.portfolio.gross_max,
        }

    def _read_status(self) -> dict[str, object]:
        try:
            out = json.loads((self.store.dir / "status.json").read_text())
            return out if isinstance(out, dict) else {}
        except (OSError, ValueError):
            return {}

    def _account(self, equity: float) -> dict[str, object]:
        if isinstance(self.broker, PaperBroker):
            return {
                "equity": equity,
                "initial": self.cfg.live.paper_initial_equity,
                "cash": self.broker.cash,
                "fees_paid": self.broker.fees_paid,
                "funding_paid": self.broker.funding_paid,
            }
        return {"equity": equity}

    def _strategy_summary(self) -> dict[str, object]:
        """The validated strategy's settings, as the dashboard documents them."""
        c = self.cfg
        pc, rc, u = c.portfolio, c.risk, c.data.universe
        return {
            "name": (self.bundle.meta.get("research") or {}).get("name")
            if isinstance(self.bundle.meta.get("research"), dict)
            else None,
            "bar": c.data.bar,
            "holding_bars": pc.holding_horizon,
            "horizons": list(c.labels.horizons),
            "target": c.labels.residualize,
            "universe_top_n": u.top_n,
            "venue": u.venue,
            "vol_target": pc.vol_target_annual,
            "gross_max": pc.gross_max,
            "net_max": pc.net_max,
            "weight_max": pc.weight_max,
            "beta_neutral": pc.beta_neutral,
            "style_neutral": pc.style_neutral,
            "cost_aversion": pc.cost_aversion,
            "books": [[b.holding_horizon, b.cost_aversion] for b in pc.books],
            "signal_halflife": pc.signal_halflife,
            "rebalance_every": pc.rebalance_every,
            "ensemble": c.model.ensemble,
            "stop_sigmas": rc.stop_loss_daily_sigmas,
            "daily_loss_limit": rc.daily_loss_limit,
            "drawdown_soft": rc.drawdown_soft,
            "drawdown_hard": rc.drawdown_hard,
            "es_limit_daily": rc.es_limit_daily,
            "max_positions": rc.max_positions,
            "capital_fraction": c.live.capital_fraction,
            "regime_gate": {
                "drawdown": pc.regime_gate_drawdown,
                "lookback_days": pc.regime_gate_lookback_days,
                "scale": pc.regime_gate_scale,
            },
            "positioning": list(c.features.positioning) if c.data.include_metrics else [],
            "funding_per_8h": c.features.funding_per_8h,
        }

    def _shortfall_bps(self, fills: list, ref: pd.Series) -> float:  # type: ignore[type-arg]
        """Implementation shortfall of this bar's fills against the decision price (the Binance close the
        decision used), notional-weighted, in bps; positive = paid more than the decision price. Includes the
        Binance/OKX basis, fees excluded. The backtest assumes about the half-spread plus impact."""
        conv = getattr(self.broker, "price_to_model", None)
        num = den = 0.0
        for f in fills:
            p0 = float(ref.get(f.symbol, np.nan))
            if not (np.isfinite(p0) and p0 > 0 and f.price > 0):
                continue
            px = conv(f.symbol, f.price) if conv else f.price
            w = abs(f.notional) if f.notional else abs(f.qty * f.price)
            num += (1.0 if f.qty > 0 else -1.0) * (px / p0 - 1.0) * w
            den += w
        return 1e4 * num / den if den > 0 else float("nan")

    def _sleeve_funding(self, panel: Panel, prices: dict[str, float]) -> None:
        """Funding settled on the sleeve's legs since its last accrual (its own P&L, which the kill rule judges)."""
        if self.sleeve is None or "funding_rate" not in panel:
            return
        last_acc = self.store.get("sleeve_funding_until")
        fr = panel["funding_rate"]
        rows = fr.iloc[-1:] if last_acc is None else fr[fr.index > pd.Timestamp(str(last_acc))]
        if not rows.empty:
            self.sleeve.accrue_funding(rows.fillna(0.0).sum().to_dict(), prices)
            self.store.put("sleeve_funding_until", str(panel.index[-1]))

    def _accrue_paper_funding(self, panel: Panel) -> None:
        """Paper account: charge every funding settlement since the last accrued bar (none is skipped when a
        cycle is late or a bar was missed)."""
        if "funding_rate" not in panel:
            return
        last_acc = self.store.get("funding_accrued_until")
        fr = panel["funding_rate"]
        if last_acc is None:
            rows = fr.iloc[-1:]
        else:
            rows = fr[fr.index > pd.Timestamp(str(last_acc))]
        if rows.empty:
            return
        self.broker.accrue_funding(rows.fillna(0.0).sum().to_dict())  # type: ignore[attr-defined]
        self.store.put("funding_accrued_until", str(panel.index[-1]))

    async def run_forever(self) -> None:
        await self.broker.start()
        await alert(
            f"Hermes démarré en mode {self.mode} (bundle {self.bundle.meta.get('config_hash')}, "
            f"promu={self.bundle.promoted})"
        )
        while True:
            bar = pd.Timedelta(BAR_TO_OFFSET[self.cfg.data.bar])
            now = pd.Timestamp.now(tz="UTC")
            nxt = now.floor(bar) + bar + pd.Timedelta(seconds=self.cfg.live.bar_close_delay_s)
            await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
            t0 = time.time()
            try:
                await self.step()
            except SystemExit:
                raise
            except Exception as exc:
                self.store.event("ERROR", f"cycle en échec : {exc!r}")
                await alert(f"cycle en échec : {exc!r}", "ERROR")
                log.exception("cycle failed")
                # The kill switch and the drawdown halt still act when the data or model path is broken.
                try:
                    await self.guard()
                except Exception:  # the broker itself may be down: nothing more to do this bar
                    log.exception("risk guard failed")
            elapsed = time.time() - t0
            self.last_cycle_s = round(elapsed, 2)
            log.info("cycle took %.1fs", elapsed)
            if elapsed > bar.total_seconds():
                self.store.event("WARNING", f"cycle de {elapsed:.0f} s, plus long qu'une bougie")

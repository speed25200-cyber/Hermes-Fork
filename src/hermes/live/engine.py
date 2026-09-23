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
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.backtest.engine import is_rebalance_bar
from hermes.config import HermesConfig, with_overrides
from hermes.data.live_feed import BinanceLiveFeed, DailyHistory
from hermes.data.panel import BAR_TO_OFFSET, Panel
from hermes.data.universe import is_excluded, universe_mask
from hermes.execution.broker import Broker, PaperBroker
from hermes.features.library import build_features
from hermes.labels.targets import build_targets
from hermes.live.alerts import alert
from hermes.live.state import StateStore
from hermes.models.bundle import ModelBundle
from hermes.models.drift import psi
from hermes.portfolio.alpha import (
    cs_zscore,
    estimate_ic,
    market_alpha_series,
    rowwise_corr,
    signal_persistence,
    smooth_scores,
)
from hermes.portfolio.construct import BookInputs, PortfolioConstructor
from hermes.portfolio.costs import CostModel
from hermes.portfolio.covariance import EwmaCovariance, market_variance
from hermes.risk.overlay import RiskOverlay, RiskState

log = logging.getLogger(__name__)

# Days of scores and realised IC kept in the state store (the IC estimate and market timing read ~90 days).
SCORE_MEMORY_DAYS = 120


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
    day, the covariance window, and never less than ``live.history_days`` when set."""
    f = cfg.features
    warm = 2 * cfg.bars(f.max_lookback_minutes) + 8 * cfg.bars(f.vol_halflife_minutes)
    cov = 2 * cfg.days(cfg.portfolio.cov_halflife_days) + 1
    floor = cfg.days(cfg.live.history_days) if cfg.live.history_days else 0
    return int(max(warm + cfg.bars_per_day, cov, floor))


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
        self.model_dir = Path(model_dir) if model_dir is not None else None
        self._model_mtime = self._bundle_mtime()
        self._cache: dict[str, pd.DataFrame] = {}
        self._drift_rows: list[tuple[pd.Timestamp, np.ndarray]] = []

    def _set_config(self, cfg: HermesConfig) -> None:
        self.cfg = cfg
        self.bpd = cfg.bars_per_day
        self.constructor = PortfolioConstructor(cfg.portfolio, cfg.bars_per_year, cfg.portfolio.ic_ref)

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
            self.store.event("ERROR", f"new bundle rejected: {exc}")
            self._model_mtime = m
            return False
        if self.mode == "live" and not new.promoted and not self.cfg.live.allow_unpromoted:
            if new.meta.get("config_hash") != self.bundle.meta.get("config_hash"):
                self.store.event("WARNING", "new bundle not promoted: kept the current one for live trading")
                self._model_mtime = m
                return False
            # Same strategy re-evaluated on more data and now failing the gate: a demotion. The engine takes
            # it, and decide() then flattens the book (live trading refuses an unpromoted bundle).
            self.store.event("ERROR", "champion demoted by its latest evaluation: live book will be flattened")
        operator = self.operator_cfg or self.cfg
        new_cfg = live_config(new.config, operator, self.overrides)
        old = self.cfg.data
        if (new_cfg.data.bar, new_cfg.data.intrabar) != (old.bar, old.intrabar) or live_history_bars(
            new_cfg
        ) > live_history_bars(self.cfg):
            self.store.event("WARNING", "new bundle needs another data feed: restarting the engine")
            raise SystemExit(3)
        self.bundle, self._model_mtime = new, m
        self._set_config(new_cfg)
        self.overlay.cfg = new_cfg.risk
        self.store.event(
            "INFO", f"bundle reloaded ({new.meta.get('config_hash')}, trained to {new.meta.get('train_end')})"
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
        return out

    def n_candidates(self) -> int:
        """Contracts watched each day: a superset of the point-in-time top-N (``live.candidates`` caps it)."""
        return min(self.cfg.live.candidates, 2 * self.cfg.data.universe.top_n + 10)

    async def refresh_candidates(self, held: list[str]) -> list[str]:
        """Today's candidates (cached per UTC day) plus anything held. Every symbol the engine may trade is
        (re-)registered with the broker on each call: after a restart the broker's contract map starts empty."""
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
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

    def _check_drift(self, ts: pd.Timestamp, X: np.ndarray, d: Decision) -> None:
        """Feature drift (PSI) of the last day's member rows against the training profile of the bundle."""
        profile = self.bundle.meta.get("feature_profile")
        if not isinstance(profile, dict) or not profile:
            return
        buf = self._drift_rows
        buf.append((ts, np.asarray(X, dtype=np.float32)))
        while buf and buf[0][0] < ts - pd.Timedelta(days=1):
            buf.pop(0)
        if len(buf) < max(4, self.bpd // 4):
            return  # too few rows for a stable histogram yet
        values = psi(profile, np.concatenate([x for _, x in buf]), self.bundle.feature_names)
        if not values:
            return
        drifted = sorted((v, k) for k, v in values.items() if v > 0.25)
        d.risk["psi_max"] = round(max(values.values()), 3)
        d.risk["psi_drifted"] = float(len(drifted))
        if len(drifted) > 0.1 * len(values):
            worst = ", ".join(k for _, k in drifted[::-1][:3])
            d.notes.append(f"feature drift vs training: {len(drifted)} features with PSI > 0.25 ({worst})")

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

    def _remember_series(self, name: str, values: pd.Series, ts: pd.Timestamp) -> None:
        cur = self._series(name, ts)
        merged = pd.concat([cur[~cur.index.isin(values.index)], values.astype(float)]).sort_index()
        self._cache[name] = merged.to_frame(name)

    def _holding_horizon(self, horizons: dict[int, object]) -> int:
        H = self.cfg.portfolio.holding_horizon
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
        X, mi = feats.stack(mask, rows=np.array([t]))
        members = list(mi.get_level_values(1))
        d.n_members = len(members)
        self._check_drift(ts, X, d)
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
        ric = self._series("ic", ts).reindex(grid)
        est = estimate_ic(ric, H, self.bundle.prior_ic, halflife_bars=self.bpd * 30)
        ic_est = float(est.iloc[-1]) if np.isfinite(est.iloc[-1]) else self.bundle.prior_ic
        cost_scale = float(self.bundle.meta.get("cost_scale", 1.0) or 1.0)  # type: ignore[arg-type]
        window = self.bpd * 30
        recent = names.reindex(pd.date_range(end=ts, periods=window + H, freq=freq))
        if recent.notna().any(axis=1).sum() > 4 * H:
            cost_scale = float(signal_persistence(recent, H, window, floor=cfg.portfolio.cost_scale_floor).iloc[-1])
        d.ic_est = round(ic_est, 5)
        d.risk["cost_scale"] = round(cost_scale, 3)
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
        row = pd.Series({s: traded.get(s, np.nan) for s in scores.index}, dtype=float)
        row = row.where(np.isfinite(row), scores.reindex(row.index))
        frame = pd.DataFrame([row.to_numpy()], index=mask.index[[t]], columns=row.index)
        zrow = cs_zscore(frame, mask.iloc[[t]].reindex(columns=row.index)).iloc[0]
        z = np.full(len(syms), np.nan)
        for s, v in zrow.items():
            z[syms.index(s)] = v
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
            ic=ic_est,
            market_alpha=m_alpha,
            sample_cov=ewma.matrix(idx),
        )
        book = self.constructor.target(inp, capital)
        target = book.weights
        if stale:
            target = RiskOverlay.restrict_to_reductions(target, pos_w[idx])
            d.notes.append("stale data: reductions only")
        if self.mode == "live" and not self.bundle.promoted and not cfg.live.allow_unpromoted:
            target = np.zeros_like(target)
            d.notes.append("bundle not promoted: live trading refused, book flattened")
        self.overlay.observe(ts, nav)
        w, info = self.overlay.apply(
            ts, nav, target, pos_w[idx], book.cov_bar, self.bpd, self._daily_returns(panel, daily, ts, idx)
        )
        d.risk.update({k: round(float(v), 5) for k, v in info.items()})
        d.risk["drawdown"] = round(self.overlay.drawdown(nav), 5)
        d.risk["halted"] = float(self.overlay.state.halted)
        d.risk["ex_ante_vol"] = round(book.ex_ante_vol_annual, 4)
        d.risk["nav"] = round(nav, 2)
        for j, i in enumerate(idx):
            if w[j] != 0 or pos_w[i] != 0:
                d.weights[syms[i]] = round(float(w[j]), 5)
                d.targets[syms[i]] = float(w[j] * capital)
        for i in np.nonzero(frozen)[0]:
            d.hold.append(syms[i])
            d.notes.append(f"{syms[i]}: missing data this bar, position left unchanged")
        for s, v in positions_notional.items():
            if s not in syms and v != 0:
                d.hold.append(s)
                d.notes.append(f"{s}: not in the data feed, position left unchanged")
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
        if positions:
            rep = await self.broker.rebalance({}, urgent=True)
            self.store.add_fills(rep.fills)
            for e in rep.errors:
                self.store.event("ERROR", e)
            await self.broker.protect({})
        self.store.write_status(
            {
                "mode": self.mode,
                "updated": pd.Timestamp.now(tz="UTC").isoformat(),
                "halted": True,
                "halt_reason": reason,
                "equity": await self.broker.equity(),
                "risk": {"halted": 1.0},
                "notes": [f"ARRÊT : {reason}"],
                "positions": {s: round(p.notional, 2) for s, p in (await self.broker.positions()).items()},
            }
        )

    async def guard(self) -> bool:
        """Risk checks that must not depend on the market-data feed or the model: kill switch and the
        drawdown halt. Returns True (after flattening) when the engine must not trade this bar."""
        ts = BinanceLiveFeed.last_closed_bar(self.cfg.data.bar)
        if self.overlay.kill_requested() and not self.overlay.state.halted:
            self.overlay.halt(ts, "kill switch file present")
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
            last = BinanceLiveFeed.last_closed_bar(self.cfg.data.bar)
            bar = pd.Timedelta(BAR_TO_OFFSET[self.cfg.data.bar])
            decision_bar = bool(is_rebalance_bar(pd.DatetimeIndex([last]), bar, every)[0])
        positions = await self.broker.positions()
        symbols = await self.refresh_candidates(list(positions))
        # Market data must arrive within the bar; otherwise the cycle fails and the guard runs again.
        budget = 0.6 * self.cfg.bar_minutes * 60.0
        panel = await asyncio.wait_for(self.feed.update(symbols), timeout=budget)
        daily = await asyncio.wait_for(self.feed.daily(symbols), timeout=budget)
        now = pd.Timestamp.now(tz="UTC")
        if isinstance(self.broker, PaperBroker):
            last = {f: panel[f].iloc[-1].dropna().to_dict() for f in ("open", "high", "low", "close")}
            stopped = self.broker.check_stops(last["high"], last["low"], last["open"])  # before the new marks
            if stopped:
                self.store.add_fills(stopped)
                self.store.event("WARNING", f"paper stops triggered: {', '.join(f.symbol for f in stopped)}")
            self.broker.set_prices(last["close"])
            self._accrue_paper_funding(panel)
        equity = await self.broker.equity()
        positions = await self.broker.positions()
        notional = {s: p.notional for s, p in positions.items()}
        d = self.decide(panel, notional, equity, now=now, daily=daily, nav=self.strategy_nav(equity))
        if not decision_bar:
            # Scores, realised IC and the smoothed signal are updated every bar, as in research; the book only
            # moves on the clock-aligned decision grid (portfolio.rebalance_every).
            d.notes.append("scoring bar: no trade (portfolio.rebalance_every)")
            return d
        if d.stale:
            await alert(f"données périmées ({d.ts}) : aucune nouvelle prise de risque", "WARNING")
        urgent = self.overlay.state.halted
        self._remember_traded([s for s, v in d.targets.items() if v != 0])
        rep = await self.broker.rebalance(d.targets, urgent=urgent, hold=set(d.hold))
        self.store.add_fills(rep.fills)
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
            for s, v in d.targets.items()
            if v != 0
        }
        await self.broker.protect(stops)
        equity_after = await self.broker.equity()
        pos_after = await self.broker.positions()
        capital_after = max(equity_after * self.cfg.live.capital_fraction, 1e-9)
        gross = sum(abs(p.notional) for p in pos_after.values()) / capital_after
        net = sum(p.notional for p in pos_after.values()) / capital_after
        ts = pd.Timestamp(d.ts)
        self.store.add_decision(ts, asdict(d))
        nav_after = float(d.risk.get("nav", equity_after))
        self.store.add_equity(ts, equity_after, gross, net, self.overlay.drawdown(nav_after), d.ic_est, len(pos_after))
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
            "bundle": {k: self.bundle.meta.get(k) for k in ("config_hash", "train_end", "promoted")},
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
                self.store.event("ERROR", f"cycle failed: {exc!r}")
                await alert(f"cycle en échec : {exc!r}", "ERROR")
                log.exception("cycle failed")
                # The kill switch and the drawdown halt still act when the data or model path is broken.
                try:
                    await self.guard()
                except Exception:  # the broker itself may be down: nothing more to do this bar
                    log.exception("risk guard failed")
            elapsed = time.time() - t0
            log.info("cycle took %.1fs", elapsed)
            if elapsed > bar.total_seconds():
                self.store.event("WARNING", f"cycle took {elapsed:.0f}s, longer than one bar")

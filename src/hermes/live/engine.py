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

from hermes.config import HermesConfig
from hermes.data.live_feed import BinanceLiveFeed
from hermes.data.panel import Panel
from hermes.data.universe import is_excluded, universe_mask
from hermes.execution.broker import Broker, PaperBroker
from hermes.features.library import build_features
from hermes.labels.targets import build_targets
from hermes.live.alerts import alert
from hermes.live.state import StateStore
from hermes.models.bundle import ModelBundle
from hermes.portfolio.alpha import estimate_ic, rowwise_corr, signal_persistence
from hermes.portfolio.construct import BookInputs, PortfolioConstructor
from hermes.portfolio.costs import CostModel
from hermes.portfolio.covariance import EwmaCovariance, market_variance
from hermes.risk.overlay import RiskOverlay, RiskState

log = logging.getLogger(__name__)


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
    ):
        if mode not in ("paper", "demo", "live"):
            raise ValueError(mode)
        self.cfg = cfg
        self.bundle = bundle
        self.feed = feed
        self.broker = broker
        self.store = store
        self.mode = mode
        self.bpd = cfg.bars_per_day
        self.constructor = PortfolioConstructor(cfg.portfolio, cfg.bars_per_year, cfg.portfolio.ic_ref)
        rs = store.get("risk_state")
        state = RiskState(**rs) if isinstance(rs, dict) else RiskState()
        if isinstance(rs, dict) and rs.get("day"):
            state.day = pd.Timestamp(rs["day"])
        self.overlay = RiskOverlay(cfg.risk, state)
        self.candidates: list[str] = list(store.get("candidates", []) or [])  # type: ignore[arg-type]
        self.candidates_day = store.get("candidates_day")
        self.model_dir = Path(model_dir) if model_dir is not None else None
        self._model_mtime = self._bundle_mtime()

    def _bundle_mtime(self) -> float:
        if self.model_dir is None or not (self.model_dir / "bundle.json").exists():
            return 0.0
        return (self.model_dir / "bundle.json").stat().st_mtime

    def maybe_reload_bundle(self) -> bool:
        """Hot-swap the champion when the retraining job installed a new one (hashes are verified)."""
        m = self._bundle_mtime()
        if not m or m == self._model_mtime or self.model_dir is None:
            return False
        try:
            new = ModelBundle.load(self.model_dir)
        except (OSError, ValueError) as exc:
            self.store.event("ERROR", f"new bundle rejected: {exc}")
            return False
        if self.mode == "live" and not new.promoted and not self.cfg.live.allow_unpromoted:
            self.store.event("WARNING", "new bundle not promoted: kept the current one for live trading")
            self._model_mtime = m
            return False
        self.bundle, self._model_mtime = new, m
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

    def tradable(self, symbols: list[str]) -> list[str]:
        u = self.cfg.data.universe
        out = [s for s in symbols if not is_excluded(s, u)]
        inst = getattr(self.broker, "instruments", None)
        if inst:  # OKX: only contracts that exist there
            out = [s for s in out if s in inst]
        return out

    async def refresh_candidates(self, held: list[str]) -> list[str]:
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        if self.feed is not None and (self.candidates_day != today or not self.candidates):
            top = await self.feed.top_symbols(self.cfg.live.candidates * 2)
            self.candidates = self.tradable(top)[: self.cfg.live.candidates]
            self.candidates_day = today
            self.store.put("candidates", self.candidates)
            self.store.put("candidates_day", today)
        return sorted(set(self.candidates) | set(held))

    # -- decision (pure given inputs; unit-testable offline) ----------------------------------------------
    def decide(
        self, panel: Panel, positions_notional: dict[str, float], equity: float, now: pd.Timestamp | None = None
    ) -> Decision:
        cfg = self.cfg
        t = len(panel.index) - 1
        ts = panel.index[t]
        expected = BinanceLiveFeed.last_closed_bar(panel.bar, now) if now is not None else ts
        stale = ts < expected
        d = Decision(ts=str(ts), equity=equity, stale=bool(stale), n_members=0, ic_est=0.0)
        mask = universe_mask(panel, cfg.data.universe, self.bpd)
        feats = build_features(panel, mask, cfg.features, self.bpd)
        if feats.names != self.bundle.feature_names:
            missing = set(self.bundle.feature_names) - set(feats.names)
            raise RuntimeError(f"feature mismatch with the bundle (missing {sorted(missing)[:5]}...)")
        X, mi = feats.stack(mask, rows=np.array([t]))
        members = list(mi.get_level_values(1))
        d.n_members = len(members)
        scores = pd.Series(self.bundle.score(X, np.zeros(len(X), dtype=np.int64)), index=members, dtype=float)
        d.scores = {k: round(float(v), 4) for k, v in scores.items() if np.isfinite(v)}
        self.store.add_scores(ts, scores)

        # Realised IC of the live scores -> causal IC estimate (prior = bundle's validation lower bound).
        H = cfg.portfolio.holding_horizon
        targets = build_targets(panel, feats, mask, cfg.labels)
        tgt = targets.residual.get(H, targets.primary)
        hist = self.store.score_history(ts - pd.Timedelta(days=120))
        ic_est = self.bundle.prior_ic
        cost_scale = float(self.bundle.meta.get("cost_scale", 1.0) or 1.0)  # type: ignore[arg-type]
        if not hist.empty:
            hist = hist.reindex(index=panel.index, columns=panel.symbols)
            ric = rowwise_corr(hist, tgt)
            est = estimate_ic(ric, H, self.bundle.prior_ic, halflife_bars=self.bpd * 30)
            ic_est = float(est.iloc[t]) if np.isfinite(est.iloc[t]) else self.bundle.prior_ic
            if hist.notna().any(axis=1).sum() > 4 * H:
                cost_scale = float(signal_persistence(hist, H, self.bpd * 30).iloc[t])
        d.ic_est = round(ic_est, 5)
        d.risk["cost_scale"] = round(cost_scale, 3)

        # Book construction on members plus anything still held.
        syms = panel.symbols
        pos_w = np.array([positions_notional.get(s, 0.0) / max(equity, 1e-9) for s in syms])
        active = mask.iloc[t].to_numpy() & np.isin(syms, members)
        idx = np.nonzero(active | (pos_w != 0))[0]
        z = np.full(len(syms), np.nan)
        for s, v in scores.items():
            z[syms.index(s)] = v
        costs = CostModel.from_panel(
            cfg.costs, panel["high"], panel["low"], panel["close"], panel["quote_volume"], feats.aux["vol"], self.bpd
        )
        r = (panel["close"] / panel["close"].shift(1) - 1).to_numpy()
        ewma = EwmaCovariance(len(syms), cfg.portfolio.cov_halflife)
        for k in range(max(0, t - cfg.portfolio.cov_halflife * 2), t + 1):
            ewma.update(np.where(mask.iloc[k].to_numpy(), r[k], np.nan))
        mvar = market_variance(feats.aux["mkt"]["mkt"], cfg.portfolio.cov_halflife // 4).iloc[t]
        dollars_typ = np.abs(pos_w[idx]).mean() * equity + 1.0 if len(idx) else 1.0
        inp = BookInputs(
            score=np.where(active[idx], z[idx], np.nan),
            ivol=feats.aux["ivol"].iloc[t].to_numpy()[idx],
            beta=feats.aux["beta"].iloc[t].to_numpy()[idx],
            mkt_var=float(mvar) if np.isfinite(mvar) else 1e-4,
            cost_rate=costs.linear_rate(t, dollars_typ)[idx] * cost_scale,
            adv=costs.adv.iloc[t].to_numpy()[idx],
            w0=pos_w[idx],
            ic=ic_est,
            sample_cov=ewma.matrix(idx),
        )
        book = self.constructor.target(inp, equity)
        target = book.weights
        if stale:
            target = RiskOverlay.restrict_to_reductions(target, pos_w[idx])
            d.notes.append("stale data: reductions only")
        if self.mode == "live" and not self.bundle.promoted and not cfg.live.allow_unpromoted:
            target = np.zeros_like(target)
            d.notes.append("bundle not promoted: live trading refused, book flattened")
        self.overlay.observe(ts, equity)
        dc = panel["close"].resample("1D").last()
        daily = (dc / dc.shift(1) - 1.0).iloc[-181:-1].to_numpy()
        w, info = self.overlay.apply(
            ts, equity, target, pos_w[idx], book.cov_bar, self.bpd, np.nan_to_num(daily[:, idx]) if len(daily) else None
        )
        d.risk.update({k: round(float(v), 5) for k, v in info.items()})
        d.risk["drawdown"] = round(self.overlay.drawdown(equity), 5)
        d.risk["halted"] = float(self.overlay.state.halted)
        d.risk["ex_ante_vol"] = round(book.ex_ante_vol_annual, 4)
        frac = cfg.live.capital_fraction
        for j, i in enumerate(idx):
            if w[j] != 0 or pos_w[i] != 0:
                d.weights[syms[i]] = round(float(w[j]), 5)
                d.targets[syms[i]] = float(w[j] * equity * frac)
        self._save_risk_state()
        return d

    # -- one cycle --------------------------------------------------------------------------------------------
    async def step(self) -> Decision:
        assert self.feed is not None
        self.maybe_reload_bundle()
        positions = await self.broker.positions()
        symbols = await self.refresh_candidates(list(positions))
        panel = await self.feed.update(symbols)
        now = pd.Timestamp.now(tz="UTC")
        if isinstance(self.broker, PaperBroker):
            self.broker.set_prices(panel["close"].iloc[-1].dropna().to_dict())
            last_acc = self.store.get("funding_accrued_until")
            ts_last = str(panel.index[-1])
            if last_acc != ts_last:
                rates = panel["funding_rate"].iloc[-1].fillna(0.0).to_dict() if "funding_rate" in panel else {}
                self.broker.accrue_funding(rates)
                self.store.put("funding_accrued_until", ts_last)
        equity = await self.broker.equity()
        positions = await self.broker.positions()
        notional = {s: p.notional for s, p in positions.items()}
        d = self.decide(panel, notional, equity, now=now)
        if d.stale:
            await alert(f"données périmées ({d.ts}) : aucune nouvelle prise de risque", "WARNING")
        urgent = self.overlay.state.halted
        rep = await self.broker.rebalance(d.targets, urgent=urgent)
        self.store.add_fills(rep.fills)
        for e in rep.errors:
            self.store.event("ERROR", e)
        # Exchange-side catastrophe stops, k daily sigmas away.
        vol = panel["close"].pipe(np.log).diff().ewm(halflife=self.cfg.features.vol_halflife, adjust=False).std()
        sig_d = (vol.iloc[-1] * np.sqrt(self.bpd)).fillna(0.05)
        stops = {
            s: float(np.clip(self.cfg.risk.stop_loss_daily_sigmas * sig_d.get(s, 0.05), 0.03, 0.5)) for s in d.targets
        }
        await self.broker.protect(stops)
        equity_after = await self.broker.equity()
        pos_after = await self.broker.positions()
        gross = sum(abs(p.notional) for p in pos_after.values()) / max(equity_after, 1e-9)
        net = sum(p.notional for p in pos_after.values()) / max(equity_after, 1e-9)
        ts = pd.Timestamp(d.ts)
        self.store.add_decision(ts, asdict(d))
        self.store.add_equity(
            ts, equity_after, gross, net, self.overlay.drawdown(equity_after), d.ic_est, len(pos_after)
        )
        status = {
            "mode": self.mode,
            "updated": now.isoformat(),
            "bar": d.ts,
            "equity": equity_after,
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
                "errors": rep.errors[:10],
            },
            "bundle": {k: self.bundle.meta.get(k) for k in ("config_hash", "train_end", "promoted")},
        }
        self.store.write_status(status)
        if self.overlay.state.halted:
            await alert(f"ARRÊT : {self.overlay.state.halt_reason}", "ERROR")
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

    async def run_forever(self) -> None:
        await self.broker.start()
        await alert(
            f"Hermes démarré en mode {self.mode} (bundle {self.bundle.meta.get('config_hash')}, "
            f"promu={self.bundle.promoted})"
        )
        bar = pd.Timedelta(self.cfg.data.bar.replace("m", "min"))
        while True:
            now = pd.Timestamp.now(tz="UTC")
            nxt = now.floor(bar) + bar + pd.Timedelta(seconds=self.cfg.live.bar_close_delay_s)
            await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
            t0 = time.time()
            try:
                await self.step()
            except Exception as exc:
                self.store.event("ERROR", f"cycle failed: {exc!r}")
                await alert(f"cycle en échec : {exc!r}", "ERROR")
                log.exception("cycle failed")
            log.info("cycle took %.1fs", time.time() - t0)

"""Assemble the modelling dataset: panel -> universe -> features -> targets -> long matrix."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hermes.config import HermesConfig
from hermes.data.panel import Panel
from hermes.data.universe import universe_mask
from hermes.features.library import FeatureSet, build_features
from hermes.labels.targets import Targets, build_targets
from hermes.models.base import cs_gauss_rank
from hermes.portfolio.costs import ADV_DAYS

log = logging.getLogger(__name__)


@dataclass
class Dataset:
    panel: Panel
    mask: pd.DataFrame
    feats: FeatureSet
    targets: Targets
    X: np.ndarray  # (rows x features) float16 storage, rows sorted by time then symbol
    t_pos: np.ndarray  # bar position of each row
    s_pos: np.ndarray  # symbol position of each row
    y: np.ndarray  # training target (Gauss-ranked blended residual return)
    y_raw: np.ndarray  # un-ranked primary residual target (evaluation)
    feature_names: list[str]
    market_X: np.ndarray  # (bars x market features) for the market-timing model
    market_y: np.ndarray  # (bars,) normalised market forward return
    market_names: list[str]

    @property
    def n_bars(self) -> int:
        return len(self.panel.index)

    def rows_for(self, bars: np.ndarray, stride: int = 1) -> np.ndarray:
        sel = np.isin(self.t_pos, bars)
        if stride > 1:
            sel &= self.t_pos % stride == 0
        return np.nonzero(sel)[0]

    def release_training_arrays(self) -> None:
        """Free what only training needs before the (forking, memory-hungry) evaluation phase."""
        self.X = np.empty((0, self.X.shape[1]), dtype=np.float16)
        self.targets.total.clear()
        self.feats.aux.pop("r1", None)
        keep = ("open", "high", "low", "close", "quote_volume", "funding_rate")
        fields = {k: v for k, v in self.panel.fields.items() if k in keep}
        # Fields the backtest never reads are aliased (no copy) to satisfy the panel's core-field contract.
        for k in ("volume", "trades", "taker_buy_quote"):
            fields[k] = fields["quote_volume"]
        self.panel = Panel(fields, bar=self.panel.bar, meta=dict(self.panel.meta))

    def to_frame(self, values: np.ndarray, rows: np.ndarray | None = None) -> pd.DataFrame:
        """Scatter long-format values back into a (time x symbol) frame."""
        out = np.full(self.mask.shape, np.nan, dtype=np.float64)
        r = np.arange(len(self.t_pos)) if rows is None else rows
        out[self.t_pos[r], self.s_pos[r]] = values
        return pd.DataFrame(out, index=self.mask.index, columns=self.mask.columns)


def blended_target(targets: Targets, horizons: tuple[int, ...]) -> pd.DataFrame:
    """Average of the vol-normalised residual targets across horizons (a horizon ensemble in one label)."""
    frames = [targets.residual[h].to_numpy() for h in horizons]
    ok = np.all(np.stack([np.isfinite(f) for f in frames]), axis=0)
    mean = np.sum(np.stack([np.nan_to_num(f) for f in frames]), axis=0) / len(frames)
    ref = targets.residual[horizons[0]]
    return pd.DataFrame(np.where(ok, mean, np.nan), index=ref.index, columns=ref.columns)


def build_dataset(panel: Panel, cfg: HermesConfig, chunk_bars: int | None = None) -> Dataset:
    mask = universe_mask(panel, cfg.data.universe)
    log.info("universe: %d bars, avg %.1f members", len(mask), mask.sum(axis=1).mean())
    if chunk_bars is None:
        # Keep one chunk's features around ~1.5 GB (float32): bars x contracts x ~150 features x 4 bytes.
        n_cols = max(1, int(mask.any(axis=0).sum()))
        chunk_bars = int(np.clip(1.5e9 / (n_cols * 150 * 4), cfg.days(20), cfg.days(365)))
    if len(panel.index) > int(chunk_bars * 1.5):
        return _build_dataset_chunked(panel, mask, cfg, chunk_bars)
    feats = build_features(panel, mask, cfg.features)
    targets = build_targets(panel, feats, mask, cfg.labels)
    y_frame = blended_target(targets, cfg.labels.horizons)

    # float16 storage halves the largest array; every consumer casts its slice back to float32.
    X, mi = feats.stack(mask, dtype=np.float16)
    t_pos = mask.index.get_indexer(mi.get_level_values(0)).astype(np.int32)
    s_pos = mask.columns.get_indexer(mi.get_level_values(1)).astype(np.int32)
    y_blend = y_frame.to_numpy()[t_pos, s_pos]
    y_raw = targets.primary.to_numpy()[t_pos, s_pos]
    y = cs_gauss_rank(y_blend, t_pos).astype(np.float32)

    market_names = list(feats.market)
    if market_names:
        market_X = np.column_stack([feats.market[k].to_numpy(np.float32) for k in market_names])
    else:
        market_X = np.zeros((len(mask), 0), np.float32)
    mh = np.stack([targets.market[h].to_numpy() for h in cfg.labels.horizons])
    market_y = np.where(np.all(np.isfinite(mh), axis=0), np.nan_to_num(mh).mean(axis=0), np.nan).astype(np.float32)
    log.info("dataset: %d rows x %d features", X.shape[0], X.shape[1])
    return Dataset(
        panel=panel,
        mask=mask,
        feats=feats,
        targets=targets,
        X=X,
        t_pos=t_pos,
        s_pos=s_pos,
        y=y,
        y_raw=y_raw.astype(np.float32),
        feature_names=feats.names,
        market_X=market_X,
        market_y=market_y,
        market_names=market_names,
    )


def _build_dataset_chunked(panel: Panel, mask: pd.DataFrame, cfg: HermesConfig, chunk_bars: int) -> Dataset:
    """Memory-bounded dataset: features are computed per time chunk (with a warm-up prefix and a forward
    suffix for the labels) on the contracts that are members during the chunk, and only member rows are
    kept. Numerically equivalent to the one-shot path up to EWMA warm-up effects (see the parity test)."""
    T, N = mask.shape
    warm = 2 * cfg.bars(cfg.features.max_lookback_minutes) + 8 * cfg.bars(cfg.features.vol_halflife_minutes)
    if cfg.labels.residualize == "style":  # the target's size exposure needs a full ADV window
        warm = max(warm, cfg.days(ADV_DAYS) + cfg.bars_per_day)
    Hmax = max(cfg.labels.horizons)
    cols = mask.columns
    aux_names = ("vol", "ivol", "beta", "r1")
    aux = {k: np.full((T, N), np.nan, dtype=np.float32) for k in aux_names}
    mkt = np.full(T, np.nan)
    mkt_vol_arr = np.full(T, np.nan)
    res = {h: np.full((T, N), np.nan, dtype=np.float32) for h in cfg.labels.horizons}
    tot = {h: np.full((T, N), np.nan, dtype=np.float32) for h in cfg.labels.horizons}
    mres = {h: np.full(T, np.nan) for h in cfg.labels.horizons}
    X_parts, t_parts, s_parts = [], [], []
    market: dict[str, np.ndarray] = {}
    names: list[str] | None = None
    for c0 in range(0, T, chunk_bars):
        c1 = min(T, c0 + chunk_bars)
        a0, a1 = max(0, c0 - warm), min(T, c1 + Hmax + 1)
        # Every contract that is a member anywhere in the computed window (warm-up and label suffix), plus BTC for the
        # market-state features: market returns and betas in the warm-up must see the same member set as the
        # one-shot computation, not only the contracts that will be members later (a survivorship leak).
        active = mask.iloc[a0:a1].any(axis=0)  # label suffix included: its market return needs the same set
        keep = [c for c in cols if active[c] or c == "BTCUSDT"]
        if not mask.iloc[c0:c1].to_numpy().any():
            continue
        sub = panel.iloc(slice(a0, a1)).subset(keep)
        m_sub = mask.iloc[a0:a1][keep]
        f = build_features(sub, m_sub, cfg.features)
        tg = build_targets(sub, f, m_sub, cfg.labels)
        names = names or f.names
        if f.names != names:
            raise RuntimeError("feature set changed between chunks")
        rows = np.arange(c0 - a0, c1 - a0)
        X, mi = f.stack(m_sub, rows=rows, dtype=np.float16)
        X_parts.append(X)
        t_parts.append(mask.index.get_indexer(mi.get_level_values(0)).astype(np.int32))
        s_parts.append(cols.get_indexer(mi.get_level_values(1)).astype(np.int32))
        col_idx = cols.get_indexer(keep)
        sl = slice(c0 - a0, c1 - a0)
        for k in aux_names:
            aux[k][c0:c1][:, col_idx] = f.aux[k].to_numpy()[sl]
        mkt[c0:c1] = f.aux["mkt"]["mkt"].to_numpy()[sl]
        mkt_vol_arr[c0:c1] = f.aux["mkt_vol"]["mkt_vol"].to_numpy()[sl]
        for h in cfg.labels.horizons:
            res[h][c0:c1][:, col_idx] = tg.residual[h].to_numpy()[sl]
            tot[h][c0:c1][:, col_idx] = tg.total[h].to_numpy()[sl]
            mres[h][c0:c1] = tg.market[h].to_numpy()[sl]
        for k, v in f.market.items():
            market.setdefault(k, np.full(T, np.nan, dtype=np.float32))[c0:c1] = v.to_numpy()[sl]
        log.info(
            "chunk %s..%s: %d rows, %d contracts", mask.index[c0].date(), mask.index[c1 - 1].date(), len(X), len(keep)
        )
        del f, tg
    assert names is not None
    X = np.concatenate(X_parts)
    t_pos = np.concatenate(t_parts)
    s_pos = np.concatenate(s_parts)
    idx = mask.index
    frame = lambda a: pd.DataFrame(a, index=idx, columns=cols)  # noqa: E731
    feats = FeatureSet(
        frames={},
        market={k: pd.Series(v, index=idx) for k, v in market.items()},
        aux={
            **{k: frame(aux[k]) for k in aux_names},
            "mkt": pd.Series(mkt, index=idx).to_frame("mkt"),
            "mkt_vol": pd.Series(mkt_vol_arr, index=idx).to_frame("mkt_vol"),
        },
    )
    feats_names = names
    targets = Targets(
        residual={h: frame(res[h]) for h in cfg.labels.horizons},
        total={h: frame(tot[h]) for h in cfg.labels.horizons},
        market={h: pd.Series(mres[h], index=idx) for h in cfg.labels.horizons},
        horizon=cfg.labels.primary_horizon,
    )
    y_frame = blended_target(targets, cfg.labels.horizons)
    y_blend = y_frame.to_numpy()[t_pos, s_pos]
    y_raw = targets.primary.to_numpy()[t_pos, s_pos]
    y = cs_gauss_rank(y_blend, t_pos).astype(np.float32)
    market_names = list(feats.market)
    market_X = (
        np.column_stack([feats.market[k].to_numpy(np.float32) for k in market_names])
        if market_names
        else np.zeros((T, 0), np.float32)
    )
    mh = np.stack([targets.market[h].to_numpy() for h in cfg.labels.horizons])
    market_y = np.where(np.all(np.isfinite(mh), axis=0), np.nan_to_num(mh).mean(axis=0), np.nan).astype(np.float32)
    log.info("dataset (chunked): %d rows x %d features", X.shape[0], X.shape[1])
    return Dataset(
        panel=panel,
        mask=mask,
        feats=feats,
        targets=targets,
        X=X,
        t_pos=t_pos,
        s_pos=s_pos,
        y=y,
        y_raw=y_raw.astype(np.float32),
        feature_names=feats_names,
        market_X=market_X,
        market_y=market_y,
        market_names=market_names,
    )

"""Features cross-actifs (§10, §35).

- ``btc_ret_hm`` / ``eth_ret_hm`` : rendements des facteurs sur bougies clôturées ALIGNÉES sur celles de
  l'instrument (mêmes ouvertures) ;
- ``rel_ret_btc_15m = ret_15m - btc_ret_15m`` ;
- ``beta_btc`` / ``beta_eth`` : régression factorielle JOINTE ridge sur les ``BETA_LOOKBACK_BARS`` derniers
  rendements 1 min du PASSÉ : ``β = (XᵀX + λI)⁻¹ Xᵀy`` avec ``X = [r_btc, r_eth]``, ``λ = shrink·tr(XᵀX)/2``.
  Une seule régression à deux facteurs : la part commune BTC/ETH n'est jamais attribuée deux fois ;
- ``residual_ret_15m = ret_15m - β_btc·btc_ret_15m - β_eth·eth_ret_15m`` ;
- coupe transversale : uniquement les instruments ADMISSIBLES du même snapshot dont les barres sont
  alignées ; ``xs_members`` publie le nombre de membres utilisés (MISSING sous ``MIN_MEMBERS``) ;
  ``xs_momentum_rank_15m`` = rang percentile de l'instrument, ``xs_breadth_15m`` = part de membres en
  hausse, ``xs_dispersion_15m`` = écart-type des rendements, ``corr_regime_60m`` = corrélation moyenne
  hors diagonale des rendements 1 min sur 60 barres.
"""

from __future__ import annotations

import numpy as np

from okxq.features.definitions import (
    FeatureDefinition,
    FeatureResult,
    MissingPolicy,
    TemporalSemantics,
    all_missing,
    invalid,
    missing,
    ok,
)
from okxq.features.state import CandleWindow, PointInTimeMarketState
from okxq.features.timeseries import contiguous_tail

GROUP = "cross_asset"
VERSION = "xs-v1"
BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
FACTOR_HORIZONS_MIN = (5, 15, 60)
BETA_LOOKBACK_BARS = 60
RIDGE_SHRINK = 0.1
XS_HORIZON_MIN = 15
CORR_LOOKBACK_BARS = 60
MIN_MEMBERS = 3


def _d(name: str, lookback_s: int, unit: str, normalization: str, description: str) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=VERSION,
        source="candle.1m (multi-instruments)",
        temporal_semantics=TemporalSemantics.CLOSED_BAR,
        lookback_s=lookback_s,
        normalization=normalization,
        unit=unit,
        missing_policy=MissingPolicy.MASK,
        group=GROUP,
        description=description,
    )


def _build() -> tuple[FeatureDefinition, ...]:
    defs: list[FeatureDefinition] = []
    for h in FACTOR_HORIZONS_MIN:
        defs.append(_d(f"btc_ret_{h}m", h * 60, "fraction", "none", f"rendement BTC {h} min"))
        defs.append(_d(f"eth_ret_{h}m", h * 60, "fraction", "none", f"rendement ETH {h} min"))
    defs.extend(
        [
            _d("rel_ret_btc_15m", 900, "fraction", "none", "rendement relatif à BTC 15 min"),
            _d("beta_btc", BETA_LOOKBACK_BARS * 60, "beta", "ridge_joint", "bêta BTC (ridge joint)"),
            _d("beta_eth", BETA_LOOKBACK_BARS * 60, "beta", "ridge_joint", "bêta ETH (ridge joint)"),
            _d("residual_ret_15m", 900, "fraction", "none", "rendement résiduel après facteurs"),
            _d("xs_members", 900, "count", "none", "membres admissibles utilisés en coupe"),
            _d("xs_momentum_rank_15m", 900, "percentile", "rank", "rang percentile du rendement 15 min"),
            _d("xs_breadth_15m", 900, "fraction", "none", "part des membres en hausse"),
            _d("xs_dispersion_15m", 900, "fraction", "none", "dispersion des rendements 15 min"),
            _d("corr_regime_60m", CORR_LOOKBACK_BARS * 60, "correlation", "none", "corrélation moyenne"),
        ]
    )
    return tuple(defs)


DEFINITIONS: tuple[FeatureDefinition, ...] = _build()
NAMES = [d.name for d in DEFINITIONS]


def _aligned_tail(own: CandleWindow, other: CandleWindow, n: int) -> tuple[CandleWindow, CandleWindow] | None:
    a = contiguous_tail(own, n)
    b = contiguous_tail(other, n)
    if a is None or b is None or not np.array_equal(a.open_ts_s, b.open_ts_s):
        return None
    return a, b


def _ret(cw: CandleWindow) -> float:
    return float(cw.close[-1]) / float(cw.close[0]) - 1.0


def ridge_joint_betas(y: np.ndarray, x: np.ndarray, shrink: float = RIDGE_SHRINK) -> np.ndarray:
    """``β = (XᵀX + λI)⁻¹ Xᵀy`` avec ``λ = shrink·tr(XᵀX)/k`` ; colonnes centrées, pas d'intercept."""
    xc = x - x.mean(axis=0)
    yc = y - y.mean()
    gram = xc.T @ xc
    k = gram.shape[0]
    lam = shrink * float(np.trace(gram)) / k
    return np.asarray(np.linalg.solve(gram + lam * np.eye(k), xc.T @ yc), dtype=float)


def compute(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    own = state.closed_candles
    if own is None or own.size == 0:
        return all_missing(NAMES, "no_closed_candles")
    out: dict[str, FeatureResult] = {}
    # l'instrument facteur lui-même sert de facteur (ses bougies propres) : pas de double source
    btc = own if state.instrument == BTC else state.peers.get(BTC)
    eth = own if state.instrument == ETH else state.peers.get(ETH)
    factor_rets: dict[tuple[str, int], float] = {}
    for label, peer in (("btc", btc), ("eth", eth)):
        for h in FACTOR_HORIZONS_MIN:
            name = f"{label}_ret_{h}m"
            if peer is None:
                out[name] = missing(f"no_{label}_candles")
                continue
            pair = _aligned_tail(own, peer, h + 1)
            if pair is None:
                out[name] = missing(f"{label}_bars_not_aligned")
                continue
            if bool(np.any(pair[1].close <= 0)):
                out[name] = invalid(f"non_positive_{label}_close")
                continue
            r = _ret(pair[1])
            factor_rets[(label, h)] = r
            out[name] = ok(r)
    own15 = contiguous_tail(own, XS_HORIZON_MIN + 1)
    own_ret15 = _ret(own15) if own15 is not None and bool(np.all(own15.close > 0)) else None
    if own_ret15 is None:
        out["rel_ret_btc_15m"] = missing("insufficient_own_bars")
    elif ("btc", 15) not in factor_rets:
        out["rel_ret_btc_15m"] = missing("no_btc_ret_15m")
    else:
        out["rel_ret_btc_15m"] = ok(own_ret15 - factor_rets[("btc", 15)])
    # bêtas joints sur le passé : rendements 1 min alignés sur BETA_LOOKBACK_BARS + 1 barres
    betas: np.ndarray | None = None
    if btc is None or eth is None:
        reason = "no_factor_candles"
        out["beta_btc"] = out["beta_eth"] = missing(reason)
    else:
        n = BETA_LOOKBACK_BARS + 1
        pb = _aligned_tail(own, btc, n)
        pe = _aligned_tail(own, eth, n)
        if pb is None or pe is None:
            out["beta_btc"] = out["beta_eth"] = missing("factor_bars_not_aligned")
        else:
            y = pb[0].close[1:] / pb[0].close[:-1] - 1.0
            xb = pb[1].close[1:] / pb[1].close[:-1] - 1.0
            xe = pe[1].close[1:] / pe[1].close[:-1] - 1.0
            x = np.column_stack([xb, xe])
            if float(np.var(xb)) == 0 or float(np.var(xe)) == 0:
                out["beta_btc"] = out["beta_eth"] = invalid("zero_factor_variance")
            else:
                betas = ridge_joint_betas(y, x)
                out["beta_btc"] = ok(float(betas[0]))
                out["beta_eth"] = ok(float(betas[1]))
    if betas is None or own_ret15 is None or ("btc", 15) not in factor_rets or ("eth", 15) not in factor_rets:
        out["residual_ret_15m"] = missing("betas_or_factor_returns_unavailable")
    else:
        out["residual_ret_15m"] = ok(
            own_ret15
            - float(betas[0]) * factor_rets[("btc", 15)]
            - float(betas[1]) * factor_rets[("eth", 15)]
        )
    # coupe transversale sur les membres admissibles du snapshot, barres alignées avec l'instrument
    members: dict[str, CandleWindow] = {}
    eligible = set(state.eligible_instruments)
    if state.instrument in eligible and own15 is not None:
        members[state.instrument] = own15
    for inst, peer in state.peers.items():
        if inst not in eligible or inst == state.instrument:
            continue
        pair = _aligned_tail(own, peer, XS_HORIZON_MIN + 1)
        if pair is not None and bool(np.all(pair[1].close > 0)):
            members[inst] = pair[1]
    count = len(members)
    out["xs_members"] = ok(float(count))
    if count < MIN_MEMBERS or state.instrument not in members:
        reason = f"members_{count}_lt_{MIN_MEMBERS}" if count < MIN_MEMBERS else "instrument_not_eligible"
        for name in ("xs_momentum_rank_15m", "xs_breadth_15m", "xs_dispersion_15m", "corr_regime_60m"):
            out[name] = missing(reason)
        return out
    names = sorted(members)
    rets = np.array([_ret(members[i]) for i in names])
    own_r = rets[names.index(state.instrument)]
    out["xs_momentum_rank_15m"] = ok(
        float(np.sum(rets < own_r) + 0.5 * (np.sum(rets == own_r) - 1)) / (count - 1)
    )
    out["xs_breadth_15m"] = ok(float(np.mean(rets > 0)))
    out["xs_dispersion_15m"] = ok(float(np.std(rets)))
    corr_rows: list[np.ndarray] = []
    for inst in names:
        src = own if inst == state.instrument else state.peers[inst]
        pair = _aligned_tail(own, src, CORR_LOOKBACK_BARS + 1)
        if pair is not None:
            corr_rows.append(pair[1].close[1:] / pair[1].close[:-1] - 1.0)
    if len(corr_rows) < MIN_MEMBERS:
        out["corr_regime_60m"] = missing("insufficient_aligned_members_60m")
    else:
        mat = np.vstack(corr_rows)
        if bool(np.any(np.std(mat, axis=1) == 0)):
            out["corr_regime_60m"] = invalid("zero_variance_member")
        else:
            c = np.corrcoef(mat)
            m = c.shape[0]
            out["corr_regime_60m"] = ok(float((np.sum(c) - m) / (m * (m - 1))))
    return out

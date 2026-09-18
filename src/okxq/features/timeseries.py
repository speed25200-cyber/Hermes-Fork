"""Features de séries temporelles sur bougies CLÔTURÉES (§9, §35).

Seules les bougies ``confirm:"1"`` dont la clôture (``ts_ms + 60 s``) est disponible à la coupure entrent
dans le calcul ; la bougie en formation n'alimente que les deux features explicitement ``intrabar``.

Formules (h en minutes, ``c`` = clôtures, dernière clôture = ``c[-1]``) :
- ``ret_hm = c[-1] / c[-1-h] - 1`` ; ``logret_hm = ln(c[-1] / c[-1-h])`` ;
- ``rv_hm = sqrt(Σ logret_1m² sur h barres)`` (non annualisée) ; ``range_hm = (max high - min low) / c[-1]`` ;
- ``volume_accel_5m_30m = mean(vol[-5:]) / mean(vol[-30:-5]) - 1`` ;
- ``vol_accel_5m_30m = rv_5m / (rv_30m * sqrt(5/30)) - 1`` ;
- ``momentum_30m_skip_5m = c[-6] / c[-31] - 1`` (saute les 5 dernières barres) ;
- ``reversal_z_15m = (c[-1] - mean(c[-15:])) / std(c[-15:])`` ;
- ``vwap_dist_30m = c[-1] / VWAP_30 - 1`` avec ``VWAP = Σ typ·vol / Σ vol``, ``typ = (h+l+c)/3`` ;
- ``dist_high_60m = c[-1] / max(high[-60:]) - 1`` ; ``dist_low_60m = c[-1] / min(low[-60:]) - 1`` ;
- ``volume_surprise_30m = (vol[-1] - mean(vol[-31:-1])) / std(vol[-31:-1])`` ;
- ``price_volume_interaction_15m = Σ ret_1m·vol / Σ vol`` sur 15 barres ;
- ``rsi_14`` : RSI de Wilder, BENCHMARK uniquement (§9).

Absence : fenêtre insuffisante → MISSING ``insufficient_bars`` ; trou dans les barres utilisées → MISSING
``bar_gap`` ; dernière clôture plus vieille que ``STALE_AFTER_BARS`` → STALE ; prix non positif → INVALID.
"""

from __future__ import annotations

import math

import numpy as np

from okxq.features.definitions import (
    FeatureDefinition,
    FeatureResult,
    MissingPolicy,
    TemporalSemantics,
    all_invalid,
    all_missing,
    invalid,
    missing,
    ok,
    stale,
)
from okxq.features.state import CandleWindow, PointInTimeMarketState

GROUP = "timeseries"
VERSION = "ts-v1"
RETURN_HORIZONS_MIN = (1, 2, 3, 5, 10, 15, 30, 60)
RV_HORIZONS_MIN = (5, 15, 30, 60)
RANGE_HORIZONS_MIN = (5, 15, 60)
MAX_BARS = 61
STALE_AFTER_BARS = 2


def _d(
    name: str,
    lookback_s: int,
    unit: str,
    normalization: str = "none",
    description: str = "",
    semantics: TemporalSemantics = TemporalSemantics.CLOSED_BAR,
) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=VERSION,
        source="candle.1m",
        temporal_semantics=semantics,
        lookback_s=lookback_s,
        normalization=normalization,
        unit=unit,
        missing_policy=MissingPolicy.STALE_BOUNDED,
        max_age_s=STALE_AFTER_BARS * 60,
        group=GROUP,
        description=description,
    )


def _build_definitions() -> tuple[FeatureDefinition, ...]:
    defs: list[FeatureDefinition] = []
    for h in RETURN_HORIZONS_MIN:
        defs.append(_d(f"ret_{h}m", h * 60, "fraction", "none", f"rendement simple sur {h} min"))
        defs.append(_d(f"logret_{h}m", h * 60, "fraction", "none", f"log-rendement sur {h} min"))
    for h in RV_HORIZONS_MIN:
        defs.append(
            _d(f"rv_{h}m", h * 60, "fraction", "none", f"volatilité réalisée {h} min (non annualisée)")
        )
    for h in RANGE_HORIZONS_MIN:
        defs.append(_d(f"range_{h}m", h * 60, "fraction", "by_close", f"amplitude high-low sur {h} min"))
    defs.extend(
        [
            _d("volume_accel_5m_30m", 1800, "ratio", "by_mean", "accélération de volume"),
            _d("vol_accel_5m_30m", 1800, "ratio", "by_scaled_rv", "accélération de volatilité"),
            _d("momentum_30m_skip_5m", 1860, "fraction", "none", "momentum 30 min sautant 5 min"),
            _d("reversal_z_15m", 900, "zscore", "zscore_window", "écart z de la clôture à sa moyenne 15 min"),
            _d("vwap_dist_30m", 1800, "fraction", "by_vwap", "distance au VWAP glissant 30 min"),
            _d("dist_high_60m", 3600, "fraction", "by_high", "distance au plus haut 60 min"),
            _d("dist_low_60m", 3600, "fraction", "by_low", "distance au plus bas 60 min"),
            _d("volume_surprise_30m", 1860, "zscore", "zscore_window", "surprise de volume"),
            _d(
                "price_volume_interaction_15m", 900, "fraction", "volume_weighted", "rendement pondéré volume"
            ),
            _d("rsi_14", 900, "score_0_100", "wilder", "RSI 14 — benchmark seulement"),
            _d(
                "intrabar_return",
                60,
                "fraction",
                "none",
                "close/open - 1 de la bougie EN FORMATION (explicitement intrabougie)",
                TemporalSemantics.INTRABAR,
            ),
            _d(
                "intrabar_age_s",
                60,
                "seconds",
                "none",
                "âge de la bougie en formation à la coupure",
                TemporalSemantics.INTRABAR,
            ),
        ]
    )
    return tuple(defs)


DEFINITIONS: tuple[FeatureDefinition, ...] = _build_definitions()
NAMES = [d.name for d in DEFINITIONS]
CLOSED_NAMES = [d.name for d in DEFINITIONS if d.temporal_semantics is TemporalSemantics.CLOSED_BAR]
INTRABAR_NAMES = [d.name for d in DEFINITIONS if d.temporal_semantics is TemporalSemantics.INTRABAR]


def contiguous_tail(candles: CandleWindow, n: int) -> CandleWindow | None:
    """Les ``n`` dernières barres si elles sont contiguës (pas de trou), sinon None."""
    if candles.size < n:
        return None
    tail = candles.tail(n)
    if n > 1 and bool(np.any(np.diff(tail.open_ts_s) != candles.bar_s)):
        return None
    return tail


def _need(candles: CandleWindow, n: int) -> tuple[CandleWindow | None, FeatureResult | None]:
    if candles.size < n:
        return None, missing(f"insufficient_bars_{candles.size}_lt_{n}")
    tail = contiguous_tail(candles, n)
    if tail is None:
        return None, missing("bar_gap")
    return tail, None


def rsi_wilder(close: np.ndarray, period: int = 14) -> float | None:
    """RSI de Wilder sur ``period`` : lissage exponentiel des gains/pertes (benchmark)."""
    if close.size < period + 1:
        return None
    diff = np.diff(close)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for g, l_ in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + float(g)) / period
        avg_loss = (avg_loss * (period - 1) + float(l_)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_closed_bar_features(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    candles = state.closed_candles
    if candles is None or candles.size == 0:
        return all_missing(CLOSED_NAMES, "no_closed_candles")
    if bool(np.any(candles.close <= 0)) or bool(np.any(candles.high < candles.low)):
        return all_invalid(CLOSED_NAMES, "non_positive_or_inconsistent_candle")
    last_close = candles.last_close_ts_s
    assert last_close is not None
    age = state.cutoff_s - last_close
    if age > STALE_AFTER_BARS * candles.bar_s:
        return {n: stale(f"last_close_age_{age:.0f}s") for n in CLOSED_NAMES}
    out: dict[str, FeatureResult] = {}
    c_last = float(candles.close[-1])
    for h in RETURN_HORIZONS_MIN:
        tail, err = _need(candles, h + 1)
        if tail is None:
            assert err is not None
            out[f"ret_{h}m"] = err
            out[f"logret_{h}m"] = err
            continue
        c0 = float(tail.close[0])
        out[f"ret_{h}m"] = ok(c_last / c0 - 1.0)
        out[f"logret_{h}m"] = ok(math.log(c_last / c0))
    rvs: dict[int, float] = {}
    for h in RV_HORIZONS_MIN:
        tail, err = _need(candles, h + 1)
        if tail is None:
            assert err is not None
            out[f"rv_{h}m"] = err
            continue
        lr = np.diff(np.log(tail.close))
        rvs[h] = math.sqrt(float(np.sum(lr * lr)))
        out[f"rv_{h}m"] = ok(rvs[h])
    for h in RANGE_HORIZONS_MIN:
        tail, err = _need(candles, h)
        if tail is None:
            assert err is not None
            out[f"range_{h}m"] = err
            continue
        out[f"range_{h}m"] = ok((float(np.max(tail.high)) - float(np.min(tail.low))) / c_last)
    tail30, err30 = _need(candles, 30)
    if tail30 is None:
        assert err30 is not None
        out["volume_accel_5m_30m"] = err30
        out["vwap_dist_30m"] = err30
    else:
        recent = float(np.mean(tail30.volume[-5:]))
        prior = float(np.mean(tail30.volume[:-5]))
        out["volume_accel_5m_30m"] = ok(recent / prior - 1.0) if prior > 0 else invalid("zero_prior_volume")
        typ = (tail30.high + tail30.low + tail30.close) / 3.0
        vol_sum = float(np.sum(tail30.volume))
        if vol_sum > 0:
            vwap = float(np.sum(typ * tail30.volume)) / vol_sum
            out["vwap_dist_30m"] = ok(c_last / vwap - 1.0)
        else:
            out["vwap_dist_30m"] = invalid("zero_volume_window")
    if 5 in rvs and 30 in rvs:
        scaled = rvs[30] * math.sqrt(5.0 / 30.0)
        out["vol_accel_5m_30m"] = ok(rvs[5] / scaled - 1.0) if scaled > 0 else invalid("zero_rv_30m")
    else:
        out["vol_accel_5m_30m"] = missing("insufficient_bars_for_rv_5m_30m")
    tail31, err31 = _need(candles, 31)
    if tail31 is None:
        assert err31 is not None
        out["momentum_30m_skip_5m"] = err31
        out["volume_surprise_30m"] = err31
    else:
        out["momentum_30m_skip_5m"] = ok(float(tail31.close[-6]) / float(tail31.close[0]) - 1.0)
        prior_vol = tail31.volume[:-1]
        sd = float(np.std(prior_vol))
        out["volume_surprise_30m"] = (
            ok((float(tail31.volume[-1]) - float(np.mean(prior_vol))) / sd)
            if sd > 0
            else invalid("zero_volume_std")
        )
    tail15, err15 = _need(candles, 15)
    if tail15 is None:
        assert err15 is not None
        out["reversal_z_15m"] = err15
        out["rsi_14"] = err15
    else:
        sd = float(np.std(tail15.close))
        out["reversal_z_15m"] = (
            ok((c_last - float(np.mean(tail15.close))) / sd) if sd > 0 else invalid("zero_close_std")
        )
        rsi = rsi_wilder(tail15.close, 14)
        out["rsi_14"] = ok(rsi) if rsi is not None else missing("insufficient_bars")
    tail16, err16 = _need(candles, 16)
    if tail16 is None:
        assert err16 is not None
        out["price_volume_interaction_15m"] = err16
    else:
        rets = tail16.close[1:] / tail16.close[:-1] - 1.0
        vols = tail16.volume[1:]
        vs = float(np.sum(vols))
        out["price_volume_interaction_15m"] = (
            ok(float(np.sum(rets * vols)) / vs) if vs > 0 else invalid("zero_volume_window")
        )
    tail60, err60 = _need(candles, 60)
    if tail60 is None:
        assert err60 is not None
        out["dist_high_60m"] = err60
        out["dist_low_60m"] = err60
    else:
        out["dist_high_60m"] = ok(c_last / float(np.max(tail60.high)) - 1.0)
        out["dist_low_60m"] = ok(c_last / float(np.min(tail60.low)) - 1.0)
    return out


def compute_intrabar_features(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    bar = state.intrabar
    if bar is None:
        return all_missing(INTRABAR_NAMES, "no_intrabar_candle")
    if bar.confirmed:
        return all_invalid(INTRABAR_NAMES, "confirmed_bar_passed_as_intrabar")
    open_s = bar.open_ts.timestamp()
    age = state.cutoff_s - open_s
    if age < 0 or age > 60:
        return all_invalid(INTRABAR_NAMES, "intrabar_not_current")
    if bar.open <= 0 or bar.close <= 0:
        return all_invalid(INTRABAR_NAMES, "non_positive_price")
    return {"intrabar_return": ok(bar.close / bar.open - 1.0), "intrabar_age_s": ok(age)}


def compute(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out = compute_closed_bar_features(state)
    out.update(compute_intrabar_features(state))
    return out

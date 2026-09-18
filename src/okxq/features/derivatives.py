"""Features de dérivés (§11, §35) : funding, open interest, basis mark/index.

Distinction ESTIMATION / RÉGLÉ (T26) :
- ``funding_rate_estimate`` : ``funding_rate`` du dernier événement ``funding`` disponible (le taux courant
  tel qu'il est connu, réglé ou non) ;
- ``funding_rate_settled_last`` : ``realized_rate`` du dernier événement ``settled:true`` dont
  ``funding_time <= cutoff``. Un événement réglé dont l'heure de règlement est POSTÉRIEURE à la coupure est
  une fuite : ``CausalityError`` (le règlement ne peut pas être connu avant d'avoir eu lieu) ;
- ``funding_rate_change = estimate - settled_last`` ;
- ``time_to_next_funding_s = next_funding_time - cutoff`` lu dans le contrat (STALE si négatif) ;
- ``oi_contracts`` : dernier OI ; ``oi_change_hm = oi_now / oi(t-h) - 1`` ;
- ``price_oi_interaction_15m = sign(ret_15m) · oi_change_15m`` (> 0 : OI s'étend dans le sens du prix) ;
- ``basis_mark_index = (mark - index) / index`` (STALE au-delà de ``PRICE_MAX_AGE_S``).
"""

from __future__ import annotations

import numpy as np

from okxq.domain.errors import CausalityError
from okxq.features.definitions import (
    FeatureDefinition,
    FeatureResult,
    MissingPolicy,
    TemporalSemantics,
    invalid,
    missing,
    ok,
    stale,
)
from okxq.features.state import FundingRecord, PointInTimeMarketState
from okxq.features.timeseries import contiguous_tail

GROUP = "derivatives"
VERSION = "deriv-v1"
PRICE_MAX_AGE_S = 60
OI_HORIZONS_MIN = (15, 60)
OI_MAX_AGE_S = 600


def _d(
    name: str, source: str, lookback_s: int, unit: str, description: str, max_age_s: int | None = None
) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=VERSION,
        source=source,
        temporal_semantics=TemporalSemantics.DERIVED,
        lookback_s=lookback_s,
        normalization="none",
        unit=unit,
        missing_policy=MissingPolicy.STALE_BOUNDED if max_age_s else MissingPolicy.MASK,
        max_age_s=max_age_s,
        group=GROUP,
        description=description,
    )


DEFINITIONS: tuple[FeatureDefinition, ...] = (
    _d(
        "funding_rate_estimate",
        "funding",
        0,
        "fraction_per_period",
        "taux courant connu (estimation ou réglé)",
    ),
    _d(
        "funding_rate_settled_last",
        "funding",
        0,
        "fraction_per_period",
        "dernier taux RÉGLÉ (règlement ≤ coupure)",
    ),
    _d("funding_rate_change", "funding", 0, "fraction_per_period", "estimation - dernier réglé"),
    _d("time_to_next_funding_s", "funding", 0, "seconds", "temps jusqu'au prochain règlement (contrat)"),
    _d("oi_contracts", "open_interest", 0, "contracts", "open interest courant", OI_MAX_AGE_S),
    _d("oi_change_15m", "open_interest", 900, "fraction", "variation OI 15 min", OI_MAX_AGE_S),
    _d("oi_change_60m", "open_interest", 3600, "fraction", "variation OI 60 min", OI_MAX_AGE_S),
    _d("price_oi_interaction_15m", "open_interest+candle.1m", 900, "fraction", "sign(ret_15m)·oi_change_15m"),
    _d(
        "basis_mark_index", "mark_price+index_price", 0, "fraction", "(mark - index) / index", PRICE_MAX_AGE_S
    ),
)
NAMES = [d.name for d in DEFINITIONS]


def latest_settled(funding: list[FundingRecord], cutoff_s: float) -> FundingRecord | None:
    """Dernier règlement CONNU ; lève CausalityError si un « réglé » précède son propre règlement (T26)."""
    settled = [f for f in funding if f.settled]
    for f in settled:
        if f.funding_time.timestamp() > cutoff_s:
            raise CausalityError(
                "taux de funding réglé utilisé avant son heure de règlement",
                funding_time=f.funding_time.isoformat(),
            )
        if f.available_at < f.funding_time:
            raise CausalityError(
                "événement funding réglé disponible avant son règlement : données incohérentes",
                funding_time=f.funding_time.isoformat(),
            )
    if not settled:
        return None
    return max(settled, key=lambda f: (f.funding_time, f.available_at))


def compute_funding(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out: dict[str, FeatureResult] = {}
    records = sorted(state.funding, key=lambda f: (f.available_at, f.ts))
    if not records:
        for n in (
            "funding_rate_estimate",
            "funding_rate_settled_last",
            "funding_rate_change",
            "time_to_next_funding_s",
        ):
            out[n] = missing("no_funding")
        return out
    last = records[-1]
    out["funding_rate_estimate"] = ok(last.funding_rate)
    settled = latest_settled(records, state.cutoff_s)
    if settled is None or settled.realized_rate is None:
        out["funding_rate_settled_last"] = missing("no_settled_funding")
        out["funding_rate_change"] = missing("no_settled_funding")
    else:
        out["funding_rate_settled_last"] = ok(settled.realized_rate)
        out["funding_rate_change"] = ok(last.funding_rate - settled.realized_rate)
    ttn = last.next_funding_time.timestamp() - state.cutoff_s
    out["time_to_next_funding_s"] = ok(ttn) if ttn >= 0 else stale("next_funding_time_in_past")
    return out


def compute_open_interest(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out: dict[str, FeatureResult] = {}
    oi = state.open_interest
    oi_names = ["oi_contracts", *[f"oi_change_{h}m" for h in OI_HORIZONS_MIN], "price_oi_interaction_15m"]
    if oi is None or oi.size == 0:
        return {n: missing("no_open_interest") for n in oi_names}
    age = state.cutoff_s - float(oi.ts_s[-1])
    if age > OI_MAX_AGE_S:
        return {n: stale(f"oi_age_{age:.0f}s") for n in oi_names}
    now = float(oi.oi_contracts[-1])
    if now <= 0:
        return {n: invalid("non_positive_oi") for n in oi_names}
    out["oi_contracts"] = ok(now)
    changes: dict[int, float] = {}
    for h in OI_HORIZONS_MIN:
        before = oi.ts_s <= state.cutoff_s - h * 60
        if not bool(np.any(before)):
            out[f"oi_change_{h}m"] = missing(f"no_oi_{h}m_ago")
            continue
        ref = float(oi.oi_contracts[before][-1])
        if ref <= 0:
            out[f"oi_change_{h}m"] = invalid("non_positive_oi_reference")
            continue
        changes[h] = now / ref - 1.0
        out[f"oi_change_{h}m"] = ok(changes[h])
    candles = state.closed_candles
    tail = contiguous_tail(candles, 16) if candles is not None else None
    if 15 not in changes:
        out["price_oi_interaction_15m"] = missing("no_oi_change_15m")
    elif tail is None or bool(np.any(tail.close <= 0)):
        out["price_oi_interaction_15m"] = missing("insufficient_bars_15m")
    else:
        ret15 = float(tail.close[-1]) / float(tail.close[0]) - 1.0
        out["price_oi_interaction_15m"] = ok(float(np.sign(ret15)) * changes[15])
    return out


def compute_basis(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    mark, index = state.mark, state.index
    if mark is None or index is None:
        return {"basis_mark_index": missing("no_mark_or_index")}
    age = max(state.cutoff_s - mark.ts.timestamp(), state.cutoff_s - index.ts.timestamp())
    if age > PRICE_MAX_AGE_S:
        return {"basis_mark_index": stale(f"mark_index_age_{age:.0f}s")}
    if mark.value <= 0 or index.value <= 0:
        return {"basis_mark_index": invalid("non_positive_mark_or_index")}
    return {"basis_mark_index": ok((mark.value - index.value) / index.value)}


def compute(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out = compute_funding(state)
    out.update(compute_open_interest(state))
    out.update(compute_basis(state))
    return out

"""Cibles (labels) par horizon (§17, §36).

Chaque exemple porte ``decision_at``, ``entry_at``, ``entry_window_end``, ``label_end_at``,
``label_available_at``, ``instrument_id``, ``side``, ``horizon_s``, ``execution_policy_version`` et
``label_quality``. Une observation dont la fenêtre dépasse les données disponibles est CENSURÉE et
masquée (T21) : ses valeurs sont nulles, jamais approximées.

Chemin de prix (``PricePath``) : DataFrame polars trié par ``ts`` avec ``ts, available_at, mid, bid,
ask, high, low, volume`` (résolution 1 min depuis les bougies clôturées et le carnet).

Définitions (``s = +1`` pour LONG/BUY, ``-1`` pour SHORT/SELL ; ``e`` = référence d'entrée = mid à
``entry_at``, première observation à ou après ``decision_at + entry_delay_s`` ; ``x`` = mid à
``label_end_at`` = dernière observation ≤ ``decision_at + horizon_s``) :
- ``future_mid_return = s·(x/e - 1)`` ;
- ``future_executable_return = future_mid_return - CostFunction.round_trip_cost(...)`` ;
- ``mfe`` / ``mae`` : extrêmes favorable/défavorable sur les barres après l'entrée, référence ``entry_mid``,
  direction ``side`` (LONG : ``max(high)/e - 1``, ``min(low)/e - 1`` ; SHORT : signes inversés) ;
- ``future_rv = sqrt(Σ Δlog(mid)²)`` sur la fenêtre ;
- ``exceeds_costs = 1{future_executable_return > 0}`` (cible de la probabilité « rendement > coûts ») ;
- politique d'exécution simulée ``exec-sim-v1`` : ordre maker posté à ``bid`` (BUY) / ``ask`` (SELL) à
  l'entrée ; rempli si une barre de la fenêtre d'entrée traverse STRICTEMENT le prix posté ;
  ``conditional_return_on_execution = s·(x/post - 1) - maker_round_trip_cost`` si rempli ;
  ``adverse_selection_after_fill = -s·(mid(fill + adverse_window_s)/post - 1)`` (> 0 = adverse) ;
- barrières : ``take_profit``/``stop_loss`` en rendement ; première barrière touchée ; les deux dans la
  même barre → AMBIGUOUS (politique ``mask`` par défaut, ou ``conservative_loss`` = perte) ; aucune →
  TIMEOUT (label 0).

``label_available_at = max(label_end_at, max available_at des observations utilisées) + label_latency_s``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.money import Side

PRICE_PATH_COLUMNS = ("ts", "available_at", "mid", "bid", "ask", "high", "low", "volume")


class CostFunction(Protocol):
    """Coûts en FRACTION du notionnel, injectables (§36). ``version`` voyage dans chaque label."""

    @property
    def version(self) -> str: ...

    def round_trip_cost(
        self, *, side: Side, entry_bid: float, entry_ask: float, exit_bid: float, exit_ask: float
    ) -> float: ...

    def maker_round_trip_cost(self, *, side: Side, exit_bid: float, exit_ask: float) -> float: ...


@dataclass(frozen=True, slots=True)
class SpreadPlusFeeCost:
    """Défaut documenté : taker à l'entrée et à la sortie = demi-spread à chaque jambe + frais taker ×2.

    Variante maker : pas de demi-spread à l'entrée (prix posté), frais maker à l'entrée, taker à la sortie.
    Aucun impact de marché n'est modélisé : c'est une BORNE BASSE des coûts, dite explicitement.
    """

    fee_taker: float = 0.0005
    fee_maker: float = 0.0002
    version: str = "cost-spread-fee-v1"

    def round_trip_cost(
        self, *, side: Side, entry_bid: float, entry_ask: float, exit_bid: float, exit_ask: float
    ) -> float:
        entry_mid = (entry_bid + entry_ask) / 2.0
        exit_mid = (exit_bid + exit_ask) / 2.0
        half_entry = (entry_ask - entry_bid) / (2.0 * entry_mid)
        half_exit = (exit_ask - exit_bid) / (2.0 * exit_mid)
        return half_entry + half_exit + 2.0 * self.fee_taker

    def maker_round_trip_cost(self, *, side: Side, exit_bid: float, exit_ask: float) -> float:
        exit_mid = (exit_bid + exit_ask) / 2.0
        return (exit_ask - exit_bid) / (2.0 * exit_mid) + self.fee_maker + self.fee_taker


class LabelQuality(StrEnum):
    OK = "OK"
    CENSORED = "CENSORED"
    NO_ENTRY = "NO_ENTRY"
    INVALID = "INVALID"


class BarrierOutcome(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIMEOUT = "TIMEOUT"
    AMBIGUOUS = "AMBIGUOUS"
    CENSORED = "CENSORED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True, slots=True)
class LabelSpec:
    horizon_s: int
    side: Side = Side.BUY
    entry_delay_s: int = 1
    entry_window_s: int = 60
    label_latency_s: int = 0
    take_profit: float | None = None
    stop_loss: float | None = None
    ambiguity_policy: Literal["mask", "conservative_loss"] = "mask"
    adverse_window_s: int = 60
    execution_policy_version: str = "exec-sim-v1"

    def __post_init__(self) -> None:
        if self.horizon_s <= 0 or self.entry_window_s <= 0 or self.entry_delay_s < 0:
            raise ValueError("horizon_s, entry_window_s > 0 et entry_delay_s ≥ 0 requis")
        if self.entry_delay_s >= self.entry_window_s:
            raise ValueError("entry_delay_s doit être inférieur à entry_window_s")
        # L'horizon se mesure depuis la DÉCISION, pas depuis l'entrée. Un horizon qui ne dépasse pas
        # la fenêtre d'entrée ne laisse donc aucune durée de détention : l'entrée et la sortie
        # tombent sur le même point, et chaque ligne sort NO_ENTRY. Refuser la combinaison ici la
        # rend impossible à déclarer, au lieu de produire un jeu de données silencieusement vide.
        if self.horizon_s <= self.entry_window_s:
            raise ValueError(
                "horizon_s doit dépasser entry_window_s : sinon aucune durée de détention ne reste "
                f"après l'entrée (horizon_s={self.horizon_s}, entry_window_s={self.entry_window_s})"
            )
        if (self.take_profit is None) != (self.stop_loss is None):
            raise ValueError("take_profit et stop_loss se configurent ensemble")
        if self.take_profit is not None and (self.take_profit <= 0 or (self.stop_loss or 0) <= 0):
            raise ValueError("barrières strictement positives")

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_s": self.horizon_s,
            "side": self.side.value,
            "entry_delay_s": self.entry_delay_s,
            "entry_window_s": self.entry_window_s,
            "label_latency_s": self.label_latency_s,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "ambiguity_policy": self.ambiguity_policy,
            "adverse_window_s": self.adverse_window_s,
            "execution_policy_version": self.execution_policy_version,
        }


LABEL_VALUE_COLUMNS = (
    "future_mid_return",
    "future_executable_return",
    "mfe",
    "mae",
    "future_rv",
    "exceeds_costs",
    "maker_filled",
    "conditional_return_on_execution",
    "adverse_selection_after_fill",
    "barrier_label",
    "round_trip_cost",
)


def validate_price_path(path: pl.DataFrame) -> pl.DataFrame:
    missing_cols = [c for c in PRICE_PATH_COLUMNS if c not in path.columns]
    if missing_cols:
        raise ValueError(f"PricePath : colonnes manquantes {missing_cols}")
    out = path.sort("ts")
    if out.height and bool((out["ts"].diff().drop_nulls() <= timedelta(0)).any()):
        raise ValueError("PricePath : horodatages dupliqués")
    return out


def _dt(ts_s: float) -> datetime:
    return datetime.fromtimestamp(ts_s, tz=UTC)


def compute_labels(
    path: pl.DataFrame,
    decisions: Sequence[datetime],
    spec: LabelSpec,
    *,
    instrument_id: str,
    cost_fn: CostFunction | None = None,
) -> pl.DataFrame:
    """Un enregistrement par décision. Les censures sont explicites (T21), jamais imputées."""
    cost = cost_fn if cost_fn is not None else SpreadPlusFeeCost()
    p = validate_price_path(path)
    ts = np.array([t.timestamp() for t in p["ts"].to_list()], dtype=float)
    avail = np.array([t.timestamp() for t in p["available_at"].to_list()], dtype=float)
    mid = p["mid"].to_numpy().astype(float)
    bid = p["bid"].to_numpy().astype(float)
    ask = p["ask"].to_numpy().astype(float)
    high = p["high"].to_numpy().astype(float)
    low = p["low"].to_numpy().astype(float)
    s = float(spec.side.sign)
    last_ts = float(ts[-1]) if ts.size else -math.inf
    rows: list[dict[str, Any]] = []
    for d in decisions:
        dec_at = ensure_utc(d)
        d_s = dec_at.timestamp()
        entry_window_end = d_s + spec.entry_window_s
        label_end = d_s + spec.horizon_s
        row: dict[str, Any] = {
            "instrument_id": instrument_id,
            "decision_at": dec_at,
            "entry_at": None,
            "entry_window_end": _dt(entry_window_end),
            "label_end_at": _dt(label_end),
            "label_available_at": None,
            "side": spec.side.value,
            "horizon_s": spec.horizon_s,
            "execution_policy_version": spec.execution_policy_version,
            "cost_version": cost.version,
            "label_quality": LabelQuality.OK.value,
            "mfe_reference": "entry_mid",
            "mfe_direction": spec.side.value,
            "barrier_outcome": BarrierOutcome.NOT_CONFIGURED.value,
            "barrier_hit_at": None,
            "maker_fill_at": None,
            **dict.fromkeys(LABEL_VALUE_COLUMNS),
        }
        if last_ts < label_end:
            row["label_quality"] = LabelQuality.CENSORED.value
            row["barrier_outcome"] = BarrierOutcome.CENSORED.value
            rows.append(row)
            continue
        e_idx = int(np.searchsorted(ts, d_s + spec.entry_delay_s, side="left"))
        if e_idx >= ts.size or ts[e_idx] > entry_window_end:
            row["label_quality"] = LabelQuality.NO_ENTRY.value
            rows.append(row)
            continue
        x_idx = int(np.searchsorted(ts, label_end, side="right")) - 1
        if x_idx <= e_idx:
            row["label_quality"] = LabelQuality.NO_ENTRY.value
            rows.append(row)
            continue
        e_mid, x_mid = float(mid[e_idx]), float(mid[x_idx])
        if e_mid <= 0 or x_mid <= 0 or bid[e_idx] <= 0 or ask[e_idx] <= 0:
            row["label_quality"] = LabelQuality.INVALID.value
            rows.append(row)
            continue
        row["entry_at"] = _dt(float(ts[e_idx]))
        used_avail = float(np.max(avail[e_idx : x_idx + 1]))
        row["label_available_at"] = _dt(max(label_end, used_avail) + spec.label_latency_s)
        mid_ret = s * (x_mid / e_mid - 1.0)
        rt_cost = cost.round_trip_cost(
            side=spec.side,
            entry_bid=float(bid[e_idx]),
            entry_ask=float(ask[e_idx]),
            exit_bid=float(bid[x_idx]),
            exit_ask=float(ask[x_idx]),
        )
        row["future_mid_return"] = mid_ret
        row["round_trip_cost"] = rt_cost
        row["future_executable_return"] = mid_ret - rt_cost
        row["exceeds_costs"] = 1.0 if mid_ret - rt_cost > 0 else 0.0
        seg_high = high[e_idx + 1 : x_idx + 1]
        seg_low = low[e_idx + 1 : x_idx + 1]
        if s > 0:
            row["mfe"] = float(np.max(seg_high)) / e_mid - 1.0
            row["mae"] = float(np.min(seg_low)) / e_mid - 1.0
        else:
            row["mfe"] = -(float(np.min(seg_low)) / e_mid - 1.0)
            row["mae"] = -(float(np.max(seg_high)) / e_mid - 1.0)
        lr = np.diff(np.log(mid[e_idx : x_idx + 1]))
        row["future_rv"] = math.sqrt(float(np.sum(lr * lr)))
        # politique maker simulée
        post = float(bid[e_idx]) if s > 0 else float(ask[e_idx])
        w_end = int(np.searchsorted(ts, entry_window_end, side="right"))
        seg_idx = np.arange(e_idx + 1, min(w_end, x_idx + 1))
        crossed = (low[seg_idx] < post) if s > 0 else (high[seg_idx] > post)
        hits = np.nonzero(crossed)[0]
        if hits.size:
            f_idx = int(seg_idx[hits[0]])
            fill_ts = float(ts[f_idx])
            row["maker_filled"] = 1.0
            row["maker_fill_at"] = _dt(fill_ts)
            row["conditional_return_on_execution"] = s * (x_mid / post - 1.0) - cost.maker_round_trip_cost(
                side=spec.side, exit_bid=float(bid[x_idx]), exit_ask=float(ask[x_idx])
            )
            a_idx = int(np.searchsorted(ts, fill_ts + spec.adverse_window_s, side="left"))
            if a_idx < ts.size and ts[a_idx] <= label_end:
                row["adverse_selection_after_fill"] = -s * (float(mid[a_idx]) / post - 1.0)
        else:
            row["maker_filled"] = 0.0
        # barrières
        if spec.take_profit is not None and spec.stop_loss is not None:
            tp_px = e_mid * (1.0 + s * spec.take_profit)
            sl_px = e_mid * (1.0 - s * spec.stop_loss)
            if s > 0:
                hit_tp = seg_high >= tp_px
                hit_sl = seg_low <= sl_px
            else:
                hit_tp = seg_low <= tp_px
                hit_sl = seg_high >= sl_px
            any_hit = np.nonzero(hit_tp | hit_sl)[0]
            if any_hit.size == 0:
                row["barrier_label"] = 0.0
                row["barrier_outcome"] = BarrierOutcome.TIMEOUT.value
            else:
                k = int(any_hit[0])
                row["barrier_hit_at"] = _dt(float(ts[e_idx + 1 + k]))
                if hit_tp[k] and hit_sl[k]:
                    row["barrier_outcome"] = BarrierOutcome.AMBIGUOUS.value
                    row["barrier_label"] = -1.0 if spec.ambiguity_policy == "conservative_loss" else None
                elif hit_tp[k]:
                    row["barrier_label"] = 1.0
                    row["barrier_outcome"] = BarrierOutcome.TAKE_PROFIT.value
                else:
                    row["barrier_label"] = -1.0
                    row["barrier_outcome"] = BarrierOutcome.STOP_LOSS.value
        rows.append(row)
    return pl.DataFrame(rows, schema=label_schema())


def label_schema() -> dict[str, pl.DataType]:
    dt = pl.Datetime("us", "UTC")
    schema: dict[str, pl.DataType] = {
        "instrument_id": pl.String(),
        "decision_at": dt,
        "entry_at": dt,
        "entry_window_end": dt,
        "label_end_at": dt,
        "label_available_at": dt,
        "side": pl.String(),
        "horizon_s": pl.Int64(),
        "execution_policy_version": pl.String(),
        "cost_version": pl.String(),
        "label_quality": pl.String(),
        "mfe_reference": pl.String(),
        "mfe_direction": pl.String(),
        "barrier_outcome": pl.String(),
        "barrier_hit_at": dt,
        "maker_fill_at": dt,
    }
    for c in LABEL_VALUE_COLUMNS:
        schema[c] = pl.Float64()
    return schema


def assert_label_causality(labels: pl.DataFrame) -> None:
    """T19 : tout label OK est disponible STRICTEMENT après sa décision et au plus tôt à la fin d'horizon."""
    ok_rows = labels.filter(pl.col("label_quality") == LabelQuality.OK.value)
    if ok_rows.height == 0:
        return
    bad = ok_rows.filter(
        (pl.col("label_available_at") <= pl.col("decision_at"))
        | (pl.col("label_available_at") < pl.col("label_end_at"))
    )
    if bad.height:
        raise ValueError(f"{bad.height} labels disponibles avant leur horizon : fuite (T19)")


def overlap_factor(horizon_s: int, sampling_s: int) -> float:
    """Chevauchement des horizons : nombre moyen de labels simultanément ouverts (``horizon / pas``)."""
    if sampling_s <= 0:
        raise ValueError("sampling_s > 0 requis")
    return max(1.0, horizon_s / sampling_s)

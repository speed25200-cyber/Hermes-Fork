"""Évaluation économique et statistique (§39.2–§39.4).

Comptabilité (documentée, simple, sans extrapolation) :
- chaque ligne (décision) est un trade hypothétique tenu ``horizon_s`` : ``gross = w·r_mid``,
  ``cost = |w|·round_trip_cost·multiplier``, ``net = gross - cost`` ;
- agrégation par horodatage de décision (moyenne sur les instruments actifs) puis division par le facteur
  de chevauchement ``horizon_s / cutoff_interval_s`` : la série obtenue est un rendement PAR INTERVALLE de
  décision sans double comptage des horizons qui se recouvrent ;
- courbe d'equity additive (somme cumulée) ; drawdown max sur cette courbe ;
- Sharpe : UNIQUEMENT sur rendements agrégés par jour UTC, annualisé par ``sqrt(365)``, et seulement si
  au moins ``MIN_DAYS_FOR_SHARPE`` jours — sinon ``None`` avec la raison. Aucune annualisation minute ;
- bootstrap temporel par blocs sur la matrice (horodatages × instruments) : les MÊMES blocs s'appliquent à
  tous les actifs ; sensibilité à la longueur de bloc ;
- contrôles négatifs : labels mélangés (un pipeline contaminé reste « prédictif » : détecté), features
  décalées vers le futur (détectées structurellement par ``available_at > decision_at`` et
  statistiquement par une corrélation implausible), coûts majorés, délais dégradés ;
- benchmarks : flat, buy-and-hold, signe du momentum, signe aléatoire à turnover égal.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import LeakageError
from okxq.research.baselines import information_coefficient

MIN_DAYS_FOR_SHARPE = 10
SHARPE_PERIODS_PER_YEAR = 365
IMPLAUSIBLE_IC = 0.5
SHUFFLED_IC_THRESHOLD = 0.1
DEFAULT_BLOCK_LENS = (12, 30, 60)


@dataclass(frozen=True, slots=True)
class SignalPolicy:
    """Poids signé depuis mu : 0 si ``|mu| <= threshold_multiple · coût`` ; sinon signe ou proportionnel borné."""

    threshold_multiple: float = 1.0
    max_weight: float = 1.0
    proportional_scale: float | None = None

    def weights(self, mu: np.ndarray, round_trip_cost: np.ndarray) -> np.ndarray:
        mu = np.asarray(mu, dtype=float)
        thr = self.threshold_multiple * np.asarray(round_trip_cost, dtype=float)
        active = np.abs(mu) > thr
        if self.proportional_scale:
            w = np.clip(mu / self.proportional_scale, -self.max_weight, self.max_weight)
        else:
            w = np.sign(mu) * self.max_weight
        return np.where(active, w, 0.0)


@dataclass
class PnlResult:
    metrics: dict[str, Any]
    equity_curve: list[tuple[datetime, float]]
    per_interval: pl.DataFrame  # decision_at, net, gross, cost, exposure
    notes: list[str] = field(default_factory=list)


def _require(frame: pl.DataFrame, cols: Sequence[str]) -> None:
    missing_cols = [c for c in cols if c not in frame.columns]
    if missing_cols:
        raise ValueError(f"colonnes manquantes pour l'évaluation : {missing_cols}")


def simulate_pnl(
    frame: pl.DataFrame,
    *,
    cutoff_interval_s: int,
    cost_multiplier: float = 1.0,
    fee_rate: float | None = None,
    weight_col: str = "weight",
    return_col: str = "future_mid_return",
    cost_col: str = "round_trip_cost",
    volume_notional_col: str | None = None,
    participation_fraction: float = 0.01,
) -> PnlResult:
    _require(frame, ["decision_at", "instrument", "horizon_s", weight_col, return_col, cost_col])
    rows = frame.filter(pl.col(return_col).is_not_null() & pl.col(cost_col).is_not_null())
    notes: list[str] = []
    if rows.height == 0:
        raise ValueError("aucune ligne évaluable (labels tous masqués)")
    if rows["horizon_s"].n_unique() != 1:
        raise ValueError("simulate_pnl attend un seul horizon par appel (documenter le chevauchement)")
    horizon_s = int(rows["horizon_s"][0])
    overlap = max(1.0, horizon_s / cutoff_interval_s)
    w = rows[weight_col].to_numpy().astype(float)
    r = rows[return_col].to_numpy().astype(float)
    c = rows[cost_col].to_numpy().astype(float) * cost_multiplier
    gross = w * r
    cost = np.abs(w) * c
    fee = np.abs(w) * (2.0 * fee_rate * cost_multiplier) if fee_rate is not None else np.zeros_like(cost)
    spread = cost - fee
    net = gross - cost
    per_row = rows.select(["decision_at", "instrument"]).with_columns(
        pl.Series("gross", gross),
        pl.Series("cost", cost),
        pl.Series("net", net),
        pl.Series("fee", fee),
        pl.Series("spread", spread),
        pl.Series("abs_w", np.abs(w)),
        pl.Series("w", w),
    )
    n_inst = per_row["instrument"].n_unique()
    per_t = (
        per_row.group_by("decision_at")
        .agg(
            (pl.col("gross").sum() / n_inst / overlap).alias("gross"),
            (pl.col("cost").sum() / n_inst / overlap).alias("cost"),
            (pl.col("net").sum() / n_inst / overlap).alias("net"),
            (pl.col("fee").sum() / n_inst / overlap).alias("fee"),
            (pl.col("spread").sum() / n_inst / overlap).alias("spread"),
            (pl.col("abs_w").sum() > 0).cast(pl.Float64).alias("exposure"),
        )
        .sort("decision_at")
    )
    net_series = per_t["net"].to_numpy().astype(float)
    equity = np.cumsum(net_series)
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    max_dd = float(-np.min(drawdown)) if drawdown.size else 0.0
    q = np.quantile(net_series, 0.05) if net_series.size else 0.0
    es = float(np.mean(net_series[net_series <= q])) if net_series.size else 0.0
    # turnover : somme des |Δw| par instrument, par intervalle de décision
    turnover = 0.0
    for _inst, g in per_row.sort("decision_at").group_by("instrument"):
        ww = g["w"].to_numpy().astype(float)
        turnover += float(np.sum(np.abs(np.diff(np.concatenate([[0.0], ww])))))
    turnover_per_interval = turnover / max(1, per_t.height) / n_inst
    pos = float(np.sum(net_series[net_series > 0]))
    neg = float(-np.sum(net_series[net_series < 0]))
    profit_factor: float | None
    if neg > 0:
        profit_factor = pos / neg
    else:
        profit_factor = None
        notes.append("profit_factor indéfini : aucune perte sur la période (non extrapolable)")
    daily = (
        per_t.with_columns(pl.col("decision_at").dt.truncate("1d").alias("day"))
        .group_by("day")
        .agg(pl.col("net").sum())
        .sort("day")
    )
    sharpe: float | None = None
    if daily.height >= MIN_DAYS_FOR_SHARPE:
        daily_net = daily["net"].to_numpy().astype(float)
        sd = float(np.std(daily_net, ddof=1))
        sharpe = float(np.mean(daily_net) / sd * np.sqrt(SHARPE_PERIODS_PER_YEAR)) if sd > 0 else None
    else:
        notes.append(
            f"sharpe non calculé : {daily.height} jour(s) < {MIN_DAYS_FOR_SHARPE} (convention 365 j, pas d'extrapolation)"
        )
    by_inst = per_row.group_by("instrument").agg(pl.col("gross").abs().sum().alias("g"))
    gross_by_inst: np.ndarray = by_inst["g"].to_numpy().astype(float)
    hhi_inst = float(np.sum((gross_by_inst / gross_by_inst.sum()) ** 2)) if gross_by_inst.sum() > 0 else None
    by_day = (
        per_t.with_columns(pl.col("decision_at").dt.truncate("1d").alias("day"))
        .group_by("day")
        .agg(pl.col("gross").abs().sum().alias("g"))
    )
    gd = by_day["g"].to_numpy().astype(float)
    hhi_day = float(np.sum((gd / gd.sum()) ** 2)) if gd.sum() > 0 else None
    capacity: float | None = None
    if volume_notional_col and volume_notional_col in rows.columns:
        vol = rows[volume_notional_col].to_numpy().astype(float)
        active = np.abs(w) > 0
        if active.any() and np.all(np.isfinite(vol[active])):
            capacity = float(np.median(vol[active]) * participation_fraction / np.max(np.abs(w[active])))
    else:
        notes.append("capacité non estimée : colonne de volume notionnel absente")
    metrics: dict[str, Any] = {
        "rows": int(rows.height),
        "instruments": int(n_inst),
        "intervals": int(per_t.height),
        "horizon_s": horizon_s,
        "overlap_factor": overlap,
        "n_effective": float(rows.height / overlap),
        "net_pnl": float(np.sum(net_series)),
        "gross_pnl": float(per_t["gross"].to_numpy().astype(float).sum()),
        "total_costs": float(per_t["cost"].to_numpy().astype(float).sum()),
        "cost_breakdown": {
            "fees": float(per_t["fee"].to_numpy().astype(float).sum()),
            "spread": float(per_t["spread"].to_numpy().astype(float).sum()),
        },
        "cost_multiplier": cost_multiplier,
        "max_drawdown": max_dd,
        "expected_shortfall_5": es,
        "turnover_per_interval": turnover_per_interval,
        "profit_factor": profit_factor,
        "fraction_time_exposed": float(per_t["exposure"].to_numpy().astype(float).mean())
        if per_t.height
        else 0.0,
        "capacity_estimate_usdt": capacity,
        "concentration_hhi_instrument": hhi_inst,
        "concentration_hhi_day": hhi_day,
        "sharpe_daily_365": sharpe,
        "days": int(daily.height),
        "mean_net_per_interval": float(np.mean(net_series)) if net_series.size else 0.0,
    }
    curve = list(zip(per_t["decision_at"].to_list(), equity.tolist(), strict=True))
    return PnlResult(metrics=metrics, equity_curve=curve, per_interval=per_t, notes=notes)


# --- bootstrap temporel par blocs communs -----------------------------------------------------------------


def _panel(per_row: pl.DataFrame, value_col: str) -> tuple[np.ndarray, list[datetime]]:
    pivot = per_row.pivot(
        on="instrument", index="decision_at", values=value_col, aggregate_function="sum"
    ).sort("decision_at")
    times = pivot["decision_at"].to_list()
    mat = pivot.drop("decision_at").to_numpy().astype(float)
    return np.nan_to_num(mat, nan=0.0), times


def block_bootstrap(
    matrix: np.ndarray,
    *,
    block_len: int,
    n_boot: int = 500,
    seed: int = 0,
    stat: Callable[[np.ndarray], float] | None = None,
) -> dict[str, float]:
    """Bootstrap par blocs circulaires sur l'axe temps ; les mêmes blocs s'appliquent à toutes les colonnes."""
    if matrix.ndim == 1:
        matrix = matrix[:, None]
    t = matrix.shape[0]
    if t < 2:
        raise ValueError("série trop courte pour un bootstrap")
    block_len = max(1, min(block_len, t))
    rng = np.random.default_rng(seed)
    f = stat or (lambda m: float(np.mean(np.sum(m, axis=1))))
    n_blocks = int(np.ceil(t / block_len))
    stats = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, t, size=n_blocks)
        idx = (starts[:, None] + np.arange(block_len)[None, :]).reshape(-1) % t
        stats[b] = f(matrix[idx[:t]])
    return {
        "point": f(matrix),
        "lower_2_5": float(np.quantile(stats, 0.025)),
        "upper_97_5": float(np.quantile(stats, 0.975)),
        "block_len": block_len,
        "n_boot": n_boot,
        "p_leq_zero": float(np.mean(stats <= 0)),
    }


def bootstrap_sensitivity(
    matrix: np.ndarray, *, block_lens: Sequence[int] = DEFAULT_BLOCK_LENS, n_boot: int = 300, seed: int = 0
) -> list[dict[str, float]]:
    return [block_bootstrap(matrix, block_len=b, n_boot=n_boot, seed=seed) for b in block_lens]


def panel_bootstrap(
    frame: pl.DataFrame,
    *,
    weight_col: str = "weight",
    return_col: str = "future_mid_return",
    cost_col: str = "round_trip_cost",
    block_lens: Sequence[int] = DEFAULT_BLOCK_LENS,
    n_boot: int = 300,
    seed: int = 0,
) -> dict[str, Any]:
    rows = frame.filter(pl.col(return_col).is_not_null() & pl.col(cost_col).is_not_null())
    w = rows[weight_col].to_numpy().astype(float)
    net = w * rows[return_col].to_numpy().astype(float) - np.abs(w) * rows[cost_col].to_numpy().astype(float)
    per_row = rows.select(["decision_at", "instrument"]).with_columns(pl.Series("net", net))
    mat, _times = _panel(per_row, "net")
    n_inst = mat.shape[1]
    stat = lambda m: float(np.mean(np.sum(m, axis=1)) / n_inst)  # noqa: E731 - rendement moyen par intervalle
    return {
        "instruments": n_inst,
        "intervals": mat.shape[0],
        "by_block_len": [
            block_bootstrap(mat, block_len=b, n_boot=n_boot, seed=seed, stat=stat) for b in block_lens
        ],
    }


# --- contrôles négatifs -------------------------------------------------------------------------------------


def assert_features_precede_decisions(
    feature_available_at: Sequence[datetime], decision_at: Sequence[datetime]
) -> None:
    """T14 : une feature disponible après la décision est une fuite structurelle."""
    for fa, d in zip(feature_available_at, decision_at, strict=True):
        if ensure_utc(fa) > ensure_utc(d):
            raise LeakageError(
                "feature disponible après la décision",
                available_at=ensure_utc(fa).isoformat(),
                decision_at=ensure_utc(d).isoformat(),
            )


def future_feature_screen(
    x: np.ndarray, y: np.ndarray, feature_names: Sequence[str], *, threshold: float = IMPLAUSIBLE_IC
) -> dict[str, Any]:
    """Corrélation feature/cible implausible (|IC| > seuil) → la feature contient probablement le futur."""
    flagged: dict[str, float] = {}
    ics: dict[str, float] = {}
    for j, name in enumerate(feature_names):
        col = x[:, j]
        ok = np.isfinite(col) & np.isfinite(y)
        if int(np.sum(ok)) < 10:
            continue
        ic = information_coefficient(col[ok], y[ok])
        ics[name] = ic
        if abs(ic) > threshold:
            flagged[name] = ic
    return {"threshold": threshold, "flagged": flagged, "leak_detected": bool(flagged), "ic_by_feature": ics}


FitPredict = Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]


def shuffled_label_control(
    fit_predict: FitPredict,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    y_test: np.ndarray,
    *,
    seed: int,
    n_rep: int = 5,
    threshold: float = SHUFFLED_IC_THRESHOLD,
) -> dict[str, Any]:
    """Avec des labels mélangés, aucun pipeline honnête ne garde de pouvoir prédictif hors échantillon.

    Un IC hors échantillon resté élevé signale une contamination (test dans l'entraînement, transformateur
    ajusté sur le test, feature contenant la cible)."""
    rng = np.random.default_rng(seed)
    genuine = information_coefficient(fit_predict(x_train, y_train, x_test), y_test)
    shuffled: list[float] = []
    for _ in range(n_rep):
        ys = rng.permutation(y_train)
        shuffled.append(information_coefficient(fit_predict(x_train, ys, x_test), y_test))
    mean_shuffled = float(np.mean(shuffled))
    return {
        "genuine_ic": genuine,
        "shuffled_ic": shuffled,
        "shuffled_ic_mean": mean_shuffled,
        "threshold": threshold,
        "leak_detected": abs(mean_shuffled) > threshold,
    }


def cost_stress(
    frame: pl.DataFrame,
    *,
    cutoff_interval_s: int,
    multipliers: Sequence[float] = (1.0, 1.5, 2.0),
    fee_rate: float | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in multipliers:
        res = simulate_pnl(frame, cutoff_interval_s=cutoff_interval_s, cost_multiplier=m, fee_rate=fee_rate)
        out.append(
            {
                "multiplier": m,
                "net_pnl": res.metrics["net_pnl"],
                "max_drawdown": res.metrics["max_drawdown"],
                "profit_factor": res.metrics["profit_factor"],
            }
        )
    return out


def latency_stress(
    frame: pl.DataFrame, *, cutoff_interval_s: int, degraded_return_col: str, fee_rate: float | None = None
) -> dict[str, Any]:
    """Délais dégradés : la colonne ``degraded_return_col`` contient le rendement relabellisé avec une entrée retardée."""
    base = simulate_pnl(frame, cutoff_interval_s=cutoff_interval_s, fee_rate=fee_rate)
    degraded = simulate_pnl(
        frame, cutoff_interval_s=cutoff_interval_s, fee_rate=fee_rate, return_col=degraded_return_col
    )
    return {
        "base_net_pnl": base.metrics["net_pnl"],
        "degraded_net_pnl": degraded.metrics["net_pnl"],
        "degradation": base.metrics["net_pnl"] - degraded.metrics["net_pnl"],
    }


# --- benchmarks §39.4 --------------------------------------------------------------------------------------


def benchmark_weights(
    frame: pl.DataFrame,
    *,
    kind: str,
    seed: int = 0,
    momentum_col: str = "ret_15m",
    turnover_like: np.ndarray | None = None,
) -> np.ndarray:
    n = frame.height
    if kind == "flat":
        return np.zeros(n)
    if kind == "buy_and_hold":
        return np.ones(n)
    if kind == "momentum_sign":
        if momentum_col not in frame.columns:
            raise ValueError(f"colonne {momentum_col} absente pour le benchmark momentum")
        m = frame[momentum_col].fill_null(0.0).to_numpy().astype(float)
        return np.sign(m)
    if kind == "random_sign":
        rng = np.random.default_rng(seed)
        w = rng.choice([-1.0, 1.0], size=n)
        if turnover_like is not None:
            active = np.abs(turnover_like) > 0
            w = np.where(active, w, 0.0)
        return w
    raise ValueError(f"benchmark inconnu : {kind}")


def benchmark_suite(
    frame: pl.DataFrame,
    *,
    cutoff_interval_s: int,
    strategy_weights: np.ndarray,
    seed: int = 0,
    fee_rate: float | None = None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for kind in ("flat", "buy_and_hold", "momentum_sign", "random_sign"):
        try:
            w = benchmark_weights(frame, kind=kind, seed=seed, turnover_like=strategy_weights)
        except ValueError as exc:
            out[kind] = {"error": str(exc)}
            continue
        res = simulate_pnl(
            frame.with_columns(pl.Series("weight", w)), cutoff_interval_s=cutoff_interval_s, fee_rate=fee_rate
        )
        out[kind] = {
            k: res.metrics[k]
            for k in (
                "net_pnl",
                "max_drawdown",
                "turnover_per_interval",
                "fraction_time_exposed",
                "profit_factor",
            )
        }
    return out

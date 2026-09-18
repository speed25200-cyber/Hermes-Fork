"""Walk-forward imbriqué, purge/embargo, période finale réservée, provenance des transformateurs (§39.1).

Règles :
- frontières COMMUNES à tous les actifs (le temps, pas les lignes) ; aucun split aléatoire ligne par
  ligne n'existe dans cette API, et ``assert_temporal_split`` refuse un entrelacement ;
- purge : ``purge_s = max(purge_s demandé, horizon max + dépendances de politique)`` ; une ligne
  d'entraînement dont ``decision_at >= train.end - purge_s`` ou dont ``label_available_at > next.start -
  embargo_s`` est PURGÉE (elle saurait quelque chose de la période suivante) ;
- la période finale (``final_test_start`` → fin) n'entre dans aucun fold ; ``FinalTestGuard`` journalise la
  première consultation (``final_test_consulted_at``) et la période perd alors son statut indépendant (T22) ;
- tout transformateur (normalisation, winsorisation, imputation, sélection, PCA, calibration) hérite de
  ``FittedTransformer`` : il enregistre sa période d'ajustement et ``assert_not_fitted_on`` lève
  ``LeakageError`` s'il a vu la période de test (T17).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Self

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import LeakageError, ProtocolViolationError


@dataclass(frozen=True, slots=True)
class Period:
    """Intervalle semi-ouvert ``[start, end)``."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start, field="start"))
        object.__setattr__(self, "end", ensure_utc(self.end, field="end"))
        if self.end <= self.start:
            raise ValueError("période vide ou inversée")

    def overlaps(self, other: Period) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, dt: datetime) -> bool:
        return self.start <= ensure_utc(dt) < self.end

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True)
class Fold:
    fold_id: str
    train: Period
    validation: Period
    test: Period
    purge_s: int
    embargo_s: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "train": self.train.to_dict(),
            "validation": self.validation.to_dict(),
            "test": self.test.to_dict(),
            "purge_s": self.purge_s,
            "embargo_s": self.embargo_s,
        }


@dataclass(frozen=True, slots=True)
class WalkForwardSpec:
    train_s: int
    validation_s: int
    test_s: int
    purge_s: int = 0
    embargo_s: int = 0
    step_s: int | None = None

    def __post_init__(self) -> None:
        if min(self.train_s, self.validation_s, self.test_s) <= 0:
            raise ValueError("train_s, validation_s, test_s > 0 requis")
        if self.purge_s < 0 or self.embargo_s < 0:
            raise ValueError("purge/embargo ≥ 0 requis")


def nested_walk_forward(
    start: datetime,
    end: datetime,
    spec: WalkForwardSpec,
    *,
    max_horizon_s: int,
    policy_dependency_s: int = 0,
    final_test_start: datetime | None = None,
) -> list[Fold]:
    """Folds successifs ``[train][validation][test]`` avançant de ``step_s`` (défaut : ``test_s``)."""
    start, end = ensure_utc(start), ensure_utc(end)
    limit = ensure_utc(final_test_start) if final_test_start is not None else end
    if limit > end:
        raise ProtocolViolationError("final_test_start après la fin des données")
    purge = max(spec.purge_s, max_horizon_s + policy_dependency_s)
    step = timedelta(seconds=spec.step_s or spec.test_s)
    folds: list[Fold] = []
    t = start
    k = 0
    while True:
        tr_end = t + timedelta(seconds=spec.train_s)
        va_end = tr_end + timedelta(seconds=spec.validation_s)
        te_end = va_end + timedelta(seconds=spec.test_s)
        if te_end > limit:
            break
        folds.append(
            Fold(
                fold_id=f"fold_{k:02d}",
                train=Period(t, tr_end),
                validation=Period(tr_end, va_end),
                test=Period(va_end, te_end),
                purge_s=purge,
                embargo_s=spec.embargo_s,
            )
        )
        k += 1
        t = t + step
    if not folds:
        raise ProtocolViolationError(
            "aucun fold walk-forward possible : période trop courte pour train+validation+test",
            available_s=(limit - start).total_seconds(),
        )
    return folds


SPLIT_TRAIN = "train"
SPLIT_VALIDATION = "validation"
SPLIT_TEST = "test"
SPLIT_PURGED = "purged"
SPLIT_OUTSIDE = "outside"


def assign_split(
    frame: pl.DataFrame,
    fold: Fold,
    *,
    decision_col: str = "decision_at",
    available_col: str = "label_available_at",
) -> pl.Series:
    """Étiquette chaque ligne : train / validation / test / purged / outside (règles de l'en-tête)."""
    d = pl.col(decision_col)
    a = pl.col(available_col)
    purge = timedelta(seconds=fold.purge_s)
    embargo = timedelta(seconds=fold.embargo_s)
    train_ok = (
        (d >= fold.train.start)
        & (d < fold.train.end - purge)
        & (a.is_null() | (a <= fold.validation.start - embargo))
    )
    train_purged = (d >= fold.train.start) & (d < fold.train.end) & ~train_ok
    val_ok = (
        (d >= fold.validation.start)
        & (d < fold.validation.end - purge)
        & (a.is_null() | (a <= fold.test.start - embargo))
    )
    val_purged = (d >= fold.validation.start) & (d < fold.validation.end) & ~val_ok
    test = (d >= fold.test.start) & (d < fold.test.end)
    expr = (
        pl.when(train_ok)
        .then(pl.lit(SPLIT_TRAIN))
        .when(train_purged)
        .then(pl.lit(SPLIT_PURGED))
        .when(val_ok)
        .then(pl.lit(SPLIT_VALIDATION))
        .when(val_purged)
        .then(pl.lit(SPLIT_PURGED))
        .when(test)
        .then(pl.lit(SPLIT_TEST))
        .otherwise(pl.lit(SPLIT_OUTSIDE))
    )
    return frame.select(expr.alias("split"))["split"]


def assert_temporal_split(
    train_times: Sequence[datetime], test_times: Sequence[datetime], *, gap_s: int = 0
) -> None:
    """Refuse tout split où une observation d'entraînement suit ou chevauche le test (pas de mélange)."""
    if not train_times or not test_times:
        return
    max_train = max(ensure_utc(t) for t in train_times)
    min_test = min(ensure_utc(t) for t in test_times)
    if max_train + timedelta(seconds=gap_s) > min_test:
        raise ProtocolViolationError(
            "split non temporel : des lignes d'entraînement suivent le début du test (split aléatoire interdit)",
            max_train=max_train.isoformat(),
            min_test=min_test.isoformat(),
        )


class FinalTestGuard:
    """Période finale réservée : une consultation est journalisée et fait perdre l'indépendance (T22)."""

    def __init__(self, period: Period) -> None:
        self.period = period
        self.consulted_at: datetime | None = None
        self.consultations: list[dict[str, str]] = []

    @property
    def independent(self) -> bool:
        return self.consulted_at is None

    def consult(self, now: datetime, *, purpose: str) -> Period:
        now = ensure_utc(now)
        if self.consulted_at is None:
            self.consulted_at = now
        self.consultations.append({"at": now.isoformat(), "purpose": purpose})
        return self.period

    def assert_untouched(self) -> None:
        if self.consulted_at is not None:
            raise ProtocolViolationError(
                "la période finale a déjà été consultée : elle n'est plus indépendante",
                consulted_at=self.consulted_at.isoformat(),
            )

    def rows(
        self, frame: pl.DataFrame, now: datetime, *, purpose: str, decision_col: str = "decision_at"
    ) -> pl.DataFrame:
        self.consult(now, purpose=purpose)
        return frame.filter(
            (pl.col(decision_col) >= self.period.start) & (pl.col(decision_col) < self.period.end)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period.to_dict(),
            "final_test_consulted_at": self.consulted_at.isoformat() if self.consulted_at else None,
            "independent": self.independent,
            "consultations": self.consultations,
        }


# --- provenance des transformateurs (T17) --------------------------------------------------------------


class FittedTransformer(ABC):
    """Base : toute transformation ajustée sur des données enregistre sa période d'ajustement."""

    kind: str = "transformer"

    def __init__(self) -> None:
        self.fit_period: Period | None = None
        self.fit_rows: int = 0

    def fit(self, x: np.ndarray, decision_at: Sequence[datetime]) -> Self:
        if x.ndim != 2 or x.shape[0] == 0:
            raise ValueError("matrice 2D non vide attendue")
        if len(decision_at) != x.shape[0]:
            raise ValueError("decision_at désaligné avec X")
        times = [ensure_utc(t) for t in decision_at]
        self.fit_period = Period(min(times), max(times) + timedelta(microseconds=1))
        self.fit_rows = int(x.shape[0])
        self._fit(x)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        if self.fit_period is None:
            raise ValueError(f"{self.kind} non ajusté")
        return self._transform(x)

    def assert_not_fitted_on(self, period: Period) -> None:
        if self.fit_period is None:
            raise LeakageError(f"{self.kind} : période d'ajustement inconnue (provenance manquante)")
        if self.fit_period.overlaps(period):
            raise LeakageError(
                f"{self.kind} ajusté sur une période qui chevauche le test",
                fit=self.fit_period.to_dict(),
                test=period.to_dict(),
            )

    @abstractmethod
    def _fit(self, x: np.ndarray) -> None: ...

    @abstractmethod
    def _transform(self, x: np.ndarray) -> np.ndarray: ...

    @abstractmethod
    def _state(self) -> dict[str, Any]: ...

    @abstractmethod
    def _load_state(self, state: dict[str, Any]) -> None: ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "fit_period": self.fit_period.to_dict() if self.fit_period else None,
            "fit_rows": self.fit_rows,
            "state": self._state(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FittedTransformer:
        klass = TRANSFORMER_KINDS[payload["kind"]]
        obj = klass.__new__(klass)
        FittedTransformer.__init__(obj)
        fp = payload.get("fit_period")
        obj.fit_period = (
            Period(datetime.fromisoformat(fp["start"]), datetime.fromisoformat(fp["end"])) if fp else None
        )
        obj.fit_rows = int(payload.get("fit_rows", 0))
        obj._load_state(payload["state"])
        return obj


class Standardizer(FittedTransformer):
    kind = "standardizer"

    def __init__(self) -> None:
        super().__init__()
        self.mean: np.ndarray = np.zeros(0)
        self.std: np.ndarray = np.zeros(0)

    def _fit(self, x: np.ndarray) -> None:
        self.mean = np.nanmean(x, axis=0)
        std = np.nanstd(x, axis=0)
        self.std = np.where(std > 0, std, 1.0)

    def _transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.std

    def _state(self) -> dict[str, Any]:
        return {"mean": self.mean.tolist(), "std": self.std.tolist()}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.mean = np.array(state["mean"], dtype=float)
        self.std = np.array(state["std"], dtype=float)


class Winsorizer(FittedTransformer):
    kind = "winsorizer"

    def __init__(self, lower_q: float = 0.01, upper_q: float = 0.99) -> None:
        super().__init__()
        if not 0 <= lower_q < upper_q <= 1:
            raise ValueError("quantiles invalides")
        self.lower_q, self.upper_q = lower_q, upper_q
        self.lower: np.ndarray = np.zeros(0)
        self.upper: np.ndarray = np.zeros(0)

    def _fit(self, x: np.ndarray) -> None:
        self.lower = np.nanquantile(x, self.lower_q, axis=0)
        self.upper = np.nanquantile(x, self.upper_q, axis=0)

    def _transform(self, x: np.ndarray) -> np.ndarray:
        return np.clip(x, self.lower, self.upper)

    def _state(self) -> dict[str, Any]:
        return {
            "lower_q": self.lower_q,
            "upper_q": self.upper_q,
            "lower": self.lower.tolist(),
            "upper": self.upper.tolist(),
        }

    def _load_state(self, state: dict[str, Any]) -> None:
        self.lower_q, self.upper_q = float(state["lower_q"]), float(state["upper_q"])
        self.lower = np.array(state["lower"], dtype=float)
        self.upper = np.array(state["upper"], dtype=float)


class MedianImputer(FittedTransformer):
    """Imputation EXPLICITE par médiane d'entraînement : la politique est nommée, datée, et enregistrée."""

    kind = "median_imputer"

    def __init__(self) -> None:
        super().__init__()
        self.median: np.ndarray = np.zeros(0)
        self.missing_fraction: np.ndarray = np.zeros(0)

    def _fit(self, x: np.ndarray) -> None:
        med = np.nanmedian(x, axis=0)
        self.median = np.where(np.isfinite(med), med, 0.0)
        self.missing_fraction = np.mean(~np.isfinite(x), axis=0)

    def _transform(self, x: np.ndarray) -> np.ndarray:
        out = np.array(x, dtype=float, copy=True)
        mask = ~np.isfinite(out)
        if mask.any():
            out[mask] = np.broadcast_to(self.median, out.shape)[mask]
        return out

    def _state(self) -> dict[str, Any]:
        return {"median": self.median.tolist(), "missing_fraction": self.missing_fraction.tolist()}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.median = np.array(state["median"], dtype=float)
        self.missing_fraction = np.array(state["missing_fraction"], dtype=float)


class VarianceSelector(FittedTransformer):
    """Sélection : retire les colonnes de variance nulle (ou trop manquantes) sur l'entraînement."""

    kind = "variance_selector"

    def __init__(self, max_missing_fraction: float = 0.5) -> None:
        super().__init__()
        self.max_missing_fraction = max_missing_fraction
        self.keep: np.ndarray = np.zeros(0, dtype=int)

    def _fit(self, x: np.ndarray) -> None:
        missing = np.mean(~np.isfinite(x), axis=0)
        var = np.nanvar(x, axis=0)
        ok = (missing <= self.max_missing_fraction) & np.isfinite(var) & (var > 0)
        self.keep = np.nonzero(ok)[0]
        if self.keep.size == 0:
            raise ValueError("aucune feature retenue par la sélection de variance")

    def _transform(self, x: np.ndarray) -> np.ndarray:
        return x[:, self.keep]

    def _state(self) -> dict[str, Any]:
        return {"max_missing_fraction": self.max_missing_fraction, "keep": self.keep.tolist()}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.max_missing_fraction = float(state["max_missing_fraction"])
        self.keep = np.array(state["keep"], dtype=int)


class PCAReducer(FittedTransformer):
    kind = "pca"

    def __init__(self, n_components: int = 5) -> None:
        super().__init__()
        self.n_components = n_components
        self.mean: np.ndarray = np.zeros(0)
        self.components: np.ndarray = np.zeros((0, 0))

    def _fit(self, x: np.ndarray) -> None:
        self.mean = np.mean(x, axis=0)
        _u, _s, vt = np.linalg.svd(x - self.mean, full_matrices=False)
        k = min(self.n_components, vt.shape[0])
        self.components = vt[:k]

    def _transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) @ self.components.T

    def _state(self) -> dict[str, Any]:
        return {
            "n_components": self.n_components,
            "mean": self.mean.tolist(),
            "components": self.components.tolist(),
        }

    def _load_state(self, state: dict[str, Any]) -> None:
        self.n_components = int(state["n_components"])
        self.mean = np.array(state["mean"], dtype=float)
        self.components = np.array(state["components"], dtype=float)


TRANSFORMER_KINDS: dict[str, type[FittedTransformer]] = {
    Standardizer.kind: Standardizer,
    Winsorizer.kind: Winsorizer,
    MedianImputer.kind: MedianImputer,
    VarianceSelector.kind: VarianceSelector,
    PCAReducer.kind: PCAReducer,
}


def register_transformer_kind(klass: type[FittedTransformer]) -> None:
    TRANSFORMER_KINDS[klass.kind] = klass


class TransformerPipeline:
    def __init__(self, steps: Sequence[FittedTransformer]) -> None:
        self.steps = list(steps)

    def fit(self, x: np.ndarray, decision_at: Sequence[datetime]) -> TransformerPipeline:
        cur = x
        for step in self.steps:
            step.fit(cur, decision_at)
            cur = step.transform(cur)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        cur = x
        for step in self.steps:
            cur = step.transform(cur)
        return cur

    def fit_transform(self, x: np.ndarray, decision_at: Sequence[datetime]) -> np.ndarray:
        return self.fit(x, decision_at).transform(x)

    def assert_not_fitted_on(self, period: Period) -> None:
        for step in self.steps:
            step.assert_not_fitted_on(period)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TransformerPipeline:
        return cls([FittedTransformer.from_dict(s) for s in payload["steps"]])

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": s.kind,
                "fit_period": s.fit_period.to_dict() if s.fit_period else None,
                "fit_rows": s.fit_rows,
            }
            for s in self.steps
        ]

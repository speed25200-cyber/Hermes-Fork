"""Covariance des rendements sur l'horizon de l'optimiseur (§51).

Estimateur : covariance empirique avec shrinkage de Ledoit-Wolf vers la cible DIAGONALE
(Schäfer & Strimmer, 2005 ; version simplifiée de Ledoit & Wolf, 2004) :

    Σ̂ = (1 − δ)·S + δ·diag(S),   δ = clip( Σ_{i≠j} Var̂(s_ij) / Σ_{i≠j} s_ij² , 0, 1 )

avec ``Var̂(s_ij)`` la variance d'échantillonnage de chaque covariance hors diagonale. δ est calculé sur
l'échantillon (pas ajusté sur la période finale) ; il peut être fixé explicitement.

Garanties :
- valeurs non finies REFUSÉES (jamais imputées) ;
- échantillon insuffisant → ``CovarianceError`` ou, si demandé, enveloppe conservatrice diagonale ;
- actifs nouveaux (absents de l'échantillon) → variance plancher conservatrice, covariance nulle
  avec les autres (documenté : c'est un choix prudent pour le terme de variance, pas une estimation) ;
- symétrie forcée, valeurs propres bornées inférieurement puis crête ``ridge`` ajoutée : la matrice
  rendue est symétrique définie positive, et la régularisation appliquée est rapportée ;
- cohérence d'horizon : les rendements sont échantillonnés sur ``sample_horizon_s`` et RAMENÉS à
  ``horizon_s`` par mise à l'échelle linéaire de la variance (hypothèse i.i.d. documentée) — l'appelant
  vérifie que ``horizon_s`` est celui des rendements attendus (§51) ;
- le modèle de risque peut être plus conservateur que le modèle d'alpha (``conservatism_multiplier``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from okxq.domain.errors import CovarianceError

DEFAULT_MIN_SAMPLES = 60
DEFAULT_RIDGE = 1e-10
DEFAULT_MIN_EIGENVALUE_FRACTION = 1e-6

InsufficientPolicy = Literal["error", "envelope"]


@dataclass(frozen=True, slots=True)
class CovarianceEstimate:
    instruments: list[str]
    matrix: np.ndarray
    horizon_s: int
    sample_horizon_s: int
    n_samples: int
    shrinkage: float
    conservatism_multiplier: float
    regularization: dict[str, float] = field(default_factory=dict)
    new_assets: list[str] = field(default_factory=list)
    source: str = "sample"

    def as_lists(self) -> list[list[float]]:
        return [[float(x) for x in row] for row in self.matrix]

    def variance_of(self, instrument: str) -> float:
        i = self.instruments.index(instrument)
        return float(self.matrix[i, i])

    def reordered(self, instruments: Sequence[str]) -> CovarianceEstimate:
        """Réordonne (et restreint) la matrice sur ``instruments`` ; un instrument absent est une erreur."""
        missing = [inst for inst in instruments if inst not in self.instruments]
        if missing:
            raise CovarianceError("instruments absents de la covariance", missing=missing)
        idx = [self.instruments.index(inst) for inst in instruments]
        return CovarianceEstimate(
            instruments=list(instruments),
            matrix=self.matrix[np.ix_(idx, idx)].copy(),
            horizon_s=self.horizon_s,
            sample_horizon_s=self.sample_horizon_s,
            n_samples=self.n_samples,
            shrinkage=self.shrinkage,
            conservatism_multiplier=self.conservatism_multiplier,
            regularization=dict(self.regularization),
            new_assets=[a for a in self.new_assets if a in instruments],
            source=self.source,
        )


def ledoit_wolf_diagonal_shrinkage(x: np.ndarray) -> float:
    """Intensité δ* de shrinkage vers la diagonale (Schäfer-Strimmer). ``x`` centré, forme (T, n)."""
    t, n = x.shape
    if n < 2 or t < 2:
        return 0.0
    s = (x.T @ x) / t
    # Variance d'échantillonnage de chaque s_ij : (1/T²)·Σ_t (x_ti·x_tj − s_ij)² × T/(T−1) (sans biais).
    var_s = np.zeros((n, n))
    for k in range(t):
        outer = np.outer(x[k], x[k])
        var_s += (outer - s) ** 2
    var_s *= t / ((t - 1.0) * t * t)
    off = ~np.eye(n, dtype=bool)
    denom = float(np.sum(s[off] ** 2))
    if denom <= 0.0:
        return 1.0
    num = float(np.sum(var_s[off]))
    return float(min(1.0, max(0.0, num / denom)))


def make_psd(
    matrix: np.ndarray,
    *,
    ridge: float = DEFAULT_RIDGE,
    min_eigenvalue_fraction: float = DEFAULT_MIN_EIGENVALUE_FRACTION,
) -> tuple[np.ndarray, dict[str, float]]:
    """Symétrise, borne les valeurs propres par ``min_eigenvalue_fraction × trace/n`` et ajoute une crête.

    Retourne la matrice réparée et la régularisation appliquée (pour le rapport).
    """
    sym = (matrix + matrix.T) / 2.0
    n = sym.shape[0]
    eigvals, eigvecs = np.linalg.eigh(sym)
    floor = max(min_eigenvalue_fraction * float(np.trace(sym)) / max(n, 1), 0.0)
    clipped = np.maximum(eigvals, floor)
    rebuilt = (eigvecs * clipped) @ eigvecs.T
    rebuilt = (rebuilt + rebuilt.T) / 2.0 + ridge * np.eye(n)
    report = {
        "min_eigenvalue_before": float(eigvals.min()) if n else 0.0,
        "eigenvalue_floor": float(floor),
        "eigenvalues_clipped": float(np.sum(eigvals < floor)),
        "ridge": float(ridge),
    }
    return rebuilt, report


def _check_finite(returns: np.ndarray) -> None:
    if returns.ndim != 2:
        raise CovarianceError("rendements attendus en matrice (T, n)", shape=list(returns.shape))
    if not np.all(np.isfinite(returns)):
        bad = np.argwhere(~np.isfinite(returns))
        raise CovarianceError(
            "rendements non finis : refusés, jamais imputés", first_bad=[int(v) for v in bad[0]]
        )


def estimate_covariance(
    instruments: Sequence[str],
    returns: np.ndarray,
    *,
    horizon_s: int,
    sample_horizon_s: int,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    shrinkage: float | None = None,
    conservatism_multiplier: float = 1.0,
    insufficient_policy: InsufficientPolicy = "error",
    envelope_variance_per_horizon: float | None = None,
    new_assets: Mapping[str, float] | None = None,
    ridge: float = DEFAULT_RIDGE,
    min_eigenvalue_fraction: float = DEFAULT_MIN_EIGENVALUE_FRACTION,
) -> CovarianceEstimate:
    """Estime Σ sur ``horizon_s`` à partir de rendements (T, n) échantillonnés sur ``sample_horizon_s``.

    - ``insufficient_policy="error"`` : T < min_samples → ``CovarianceError`` ;
      ``"envelope"`` : matrice diagonale ``envelope_variance_per_horizon`` (variance CONSERVATRICE par
      actif sur ``horizon_s``, fournie par l'appelant) — jamais une estimation bruitée maquillée ;
    - ``new_assets`` : instrument → variance plancher sur ``horizon_s`` ; la variance retenue est
      ``max(plancher, max des variances observées)`` (prudence) ; covariances nulles avec le reste.
    """
    inst = list(instruments)
    if len(set(inst)) != len(inst):
        raise CovarianceError("instruments dupliqués")
    if horizon_s <= 0 or sample_horizon_s <= 0:
        raise CovarianceError("horizons non positifs", horizon_s=horizon_s, sample_horizon_s=sample_horizon_s)
    if conservatism_multiplier < 1.0:
        raise CovarianceError("conservatism_multiplier doit être ≥ 1", value=conservatism_multiplier)
    x = np.asarray(returns, dtype=float)
    _check_finite(x)
    t, n = x.shape
    if n != len(inst):
        raise CovarianceError(
            "nombre de colonnes différent des instruments", columns=n, instruments=len(inst)
        )
    scale = float(horizon_s) / float(sample_horizon_s)
    regularization: dict[str, float] = {}

    if t < min_samples:
        if insufficient_policy == "error":
            raise CovarianceError(
                "échantillon insuffisant pour la covariance", n_samples=t, min_samples=min_samples
            )
        if envelope_variance_per_horizon is None or not np.isfinite(envelope_variance_per_horizon):
            raise CovarianceError("enveloppe conservatrice demandée sans variance d'enveloppe")
        if envelope_variance_per_horizon <= 0:
            raise CovarianceError("variance d'enveloppe non positive")
        base = np.eye(n) * float(envelope_variance_per_horizon) * conservatism_multiplier
        delta = 1.0
        source = "envelope"
    else:
        centered = x - x.mean(axis=0, keepdims=True)
        sample = (centered.T @ centered) / max(t - 1, 1)
        delta = ledoit_wolf_diagonal_shrinkage(centered) if shrinkage is None else float(shrinkage)
        if not (0.0 <= delta <= 1.0):
            raise CovarianceError("shrinkage hors [0, 1]", shrinkage=delta)
        target = np.diag(np.diag(sample))
        base = ((1.0 - delta) * sample + delta * target) * scale * conservatism_multiplier
        source = "sample"

    all_instruments = list(inst)
    new_names: list[str] = []
    if new_assets:
        observed_max = float(np.max(np.diag(base))) if n else 0.0
        extra: list[float] = []
        for name, floor_var in new_assets.items():
            if name in all_instruments:
                raise CovarianceError("actif nouveau déjà présent dans l'échantillon", instrument=name)
            if not np.isfinite(floor_var) or floor_var <= 0:
                raise CovarianceError("variance plancher invalide pour un actif nouveau", instrument=name)
            all_instruments.append(name)
            new_names.append(name)
            extra.append(max(float(floor_var), observed_max) * conservatism_multiplier)
        m = len(all_instruments)
        grown = np.zeros((m, m))
        grown[:n, :n] = base
        for k, v in enumerate(extra):
            grown[n + k, n + k] = v
        base = grown

    matrix, psd_report = make_psd(base, ridge=ridge, min_eigenvalue_fraction=min_eigenvalue_fraction)
    regularization.update(psd_report)
    if not np.all(np.isfinite(matrix)):
        raise CovarianceError("matrice non finie après régularisation")
    return CovarianceEstimate(
        instruments=all_instruments,
        matrix=matrix,
        horizon_s=horizon_s,
        sample_horizon_s=sample_horizon_s,
        n_samples=t,
        shrinkage=float(delta),
        conservatism_multiplier=float(conservatism_multiplier),
        regularization=regularization,
        new_assets=new_names,
        source=source,
    )


def assert_covariance_horizon(estimate: CovarianceEstimate, horizon_s: int) -> None:
    """La covariance et les rendements attendus doivent porter sur le MÊME horizon (§51)."""
    if estimate.horizon_s != horizon_s:
        raise CovarianceError(
            "horizon de covariance différent de l'horizon des rendements attendus",
            covariance_horizon_s=estimate.horizon_s,
            expected_horizon_s=horizon_s,
        )


def is_symmetric_psd(matrix: np.ndarray, *, tol: float = 1e-12) -> bool:
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        return False
    if not np.all(np.isfinite(matrix)):
        return False
    if not np.allclose(matrix, matrix.T, atol=tol, rtol=0.0):
        return False
    return bool(np.linalg.eigvalsh((matrix + matrix.T) / 2.0).min() >= -tol)

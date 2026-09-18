"""Baselines et famille LightGBM verrouillée (§39.4).

Tous les modèles se sérialisent en JSON/texte (LightGBM : ``model_to_string``) — jamais de pickle. Chaque
modèle expose ``family``, ``task`` (``regression`` → mu en rendement ; ``classification`` → probabilité),
``hyperparameters`` et ``fit/predict``. Le benchmark ``flat`` est le point de comparaison « ne rien
faire » : ce n'est pas un modèle candidat, il est étiqueté ``is_benchmark``.

Les baselines momentum et retour à la moyenne sont des modèles à UN paramètre estimé sur le passé
(pente OLS d'une feature nommée) : leur signe et leur amplitude viennent des données d'entraînement.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, Literal

import numpy as np

Task = Literal["regression", "classification"]

LOCKED_LGBM_PARAMS_V1: dict[str, Any] = {
    "num_leaves": 15,
    "learning_rate": 0.05,
    "n_estimators": 200,
    "min_child_samples": 50,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "max_depth": 6,
    "num_threads": 2,
    "verbosity": -1,
    "deterministic": True,
    "force_row_wise": True,
}
LOCKED_LGBM_VERSION = "lgbm-locked-v1"


class BaseModel(ABC):
    family: str = "base"
    task: Task = "regression"
    is_benchmark: bool = False

    def __init__(self, **hyperparameters: Any) -> None:
        self.hyperparameters: dict[str, Any] = dict(hyperparameters)
        self.fitted = False
        self.n_features: int = 0
        self.train_rows: int = 0

    @property
    def name(self) -> str:
        hp = ",".join(f"{k}={v}" for k, v in sorted(self.hyperparameters.items()) if k != "seed")
        return f"{self.family}({hp})" if hp else self.family

    def fit(self, x: np.ndarray, y: np.ndarray) -> BaseModel:
        if x.ndim != 2 or y.ndim != 1 or x.shape[0] != y.shape[0]:
            raise ValueError("X 2D et y 1D alignés requis")
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
            raise ValueError("X/y doivent être finis : imputer explicitement avant l'entraînement")
        self.n_features = int(x.shape[1])
        self.train_rows = int(x.shape[0])
        self._fit(x, y)
        self.fitted = True
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        if not self.fitted:
            raise ValueError(f"{self.family} non ajusté")
        if x.ndim != 2 or x.shape[1] != self.n_features:
            raise ValueError(f"{self.family} : {self.n_features} features attendues, {x.shape} reçu")
        out = np.asarray(self._predict(x), dtype=float)
        if not np.all(np.isfinite(out)):
            raise ValueError(f"{self.family} : prédiction non finie")
        return out

    @abstractmethod
    def _fit(self, x: np.ndarray, y: np.ndarray) -> None: ...

    @abstractmethod
    def _predict(self, x: np.ndarray) -> np.ndarray: ...

    @abstractmethod
    def _state(self) -> dict[str, Any]: ...

    @abstractmethod
    def _load_state(self, state: dict[str, Any]) -> None: ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "task": self.task,
            "hyperparameters": self.hyperparameters,
            "n_features": self.n_features,
            "train_rows": self.train_rows,
            "fitted": self.fitted,
            "state": self._state(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BaseModel:
        klass = MODEL_FAMILIES[payload["family"]]
        obj = klass(**payload.get("hyperparameters", {}))
        obj.n_features = int(payload["n_features"])
        obj.train_rows = int(payload.get("train_rows", 0))
        obj._load_state(payload["state"])
        obj.fitted = bool(payload.get("fitted", True))
        return obj


class FlatNoTradeBenchmark(BaseModel):
    """Benchmark « ne rien faire » : mu = 0 partout. Sert de référence, jamais de candidat."""

    family = "flat"
    is_benchmark = True

    def _fit(self, x: np.ndarray, y: np.ndarray) -> None:
        return None

    def _predict(self, x: np.ndarray) -> np.ndarray:
        return np.zeros(x.shape[0])

    def _state(self) -> dict[str, Any]:
        return {}

    def _load_state(self, state: dict[str, Any]) -> None:
        return None


class RidgeModel(BaseModel):
    """Ridge avec intercept : ``β = (XᵀX + αI)⁻¹ Xᵀy`` sur colonnes centrées."""

    family = "ridge"

    def __init__(self, alpha: float = 1.0) -> None:
        super().__init__(alpha=float(alpha))
        self.coef: np.ndarray = np.zeros(0)
        self.intercept: float = 0.0
        self.x_mean: np.ndarray = np.zeros(0)

    def _fit(self, x: np.ndarray, y: np.ndarray) -> None:
        alpha = float(self.hyperparameters["alpha"])
        self.x_mean = x.mean(axis=0)
        xc = x - self.x_mean
        y_mean = float(y.mean())
        gram = xc.T @ xc + alpha * np.eye(x.shape[1])
        self.coef = np.linalg.solve(gram, xc.T @ (y - y_mean))
        self.intercept = y_mean

    def _predict(self, x: np.ndarray) -> np.ndarray:
        return (x - self.x_mean) @ self.coef + self.intercept

    def _state(self) -> dict[str, Any]:
        return {"coef": self.coef.tolist(), "intercept": self.intercept, "x_mean": self.x_mean.tolist()}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.coef = np.array(state["coef"], dtype=float)
        self.intercept = float(state["intercept"])
        self.x_mean = np.array(state["x_mean"], dtype=float)


class LogisticModel(BaseModel):
    """Régression logistique L2 (scikit-learn, lbfgs) ; ``predict`` rend P(y=1)."""

    family = "logistic"
    task = "classification"

    def __init__(self, c: float = 1.0, max_iter: int = 300) -> None:
        super().__init__(c=float(c), max_iter=int(max_iter))
        self.coef: np.ndarray = np.zeros(0)
        self.intercept: float = 0.0
        self.constant_class: float | None = None

    def _fit(self, x: np.ndarray, y: np.ndarray) -> None:
        classes = np.unique(y)
        if classes.size < 2:
            self.constant_class = float(classes[0])
            self.coef = np.zeros(x.shape[1])
            self.intercept = 0.0
            return
        from sklearn.linear_model import LogisticRegression

        model = LogisticRegression(
            C=float(self.hyperparameters["c"]), max_iter=int(self.hyperparameters["max_iter"])
        )
        model.fit(x, y.astype(int))
        self.coef = np.asarray(model.coef_[0], dtype=float)
        self.intercept = float(model.intercept_[0])
        self.constant_class = None

    def _predict(self, x: np.ndarray) -> np.ndarray:
        if self.constant_class is not None:
            return np.full(x.shape[0], 0.5 + 0.5 * (1.0 if self.constant_class > 0 else -1.0) * 0.98)
        z = x @ self.coef + self.intercept
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def _state(self) -> dict[str, Any]:
        return {
            "coef": self.coef.tolist(),
            "intercept": self.intercept,
            "constant_class": self.constant_class,
        }

    def _load_state(self, state: dict[str, Any]) -> None:
        self.coef = np.array(state["coef"], dtype=float)
        self.intercept = float(state["intercept"])
        self.constant_class = state.get("constant_class")


class SingleFeatureSlopeModel(BaseModel):
    """``mu = k · x[:, feature_index]`` avec ``k`` estimé par OLS sans intercept sur l'entraînement."""

    family = "single_feature_slope"

    def __init__(self, feature_index: int = 0, sign: int = 1) -> None:
        super().__init__(feature_index=int(feature_index), sign=int(sign))
        self.k: float = 0.0

    def _fit(self, x: np.ndarray, y: np.ndarray) -> None:
        f = x[:, int(self.hyperparameters["feature_index"])]
        denom = float(f @ f)
        self.k = float(f @ y) / denom if denom > 0 else 0.0

    def _predict(self, x: np.ndarray) -> np.ndarray:
        return self.k * x[:, int(self.hyperparameters["feature_index"])]

    def _state(self) -> dict[str, Any]:
        return {"k": self.k}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.k = float(state["k"])


class MomentumBaseline(SingleFeatureSlopeModel):
    """Momentum simple : pente OLS du rendement futur sur une feature de momentum (``sign=+1`` attendu)."""

    family = "momentum"


class MeanReversionBaseline(SingleFeatureSlopeModel):
    """Retour à la moyenne simple : pente OLS sur un écart z (``sign=-1`` attendu si la thèse tient)."""

    family = "mean_reversion"


class LockedLightGBM(BaseModel):
    """Famille LightGBM à paramètres FIGÉS (``LOCKED_LGBM_PARAMS_V1``) ; seule la graine varie."""

    family = "lightgbm_locked"

    def __init__(self, task: Task = "regression", seed: int = 25200) -> None:
        super().__init__(task=task, seed=int(seed), locked_version=LOCKED_LGBM_VERSION)
        self.task = task
        self.model_text: str = ""
        self._booster: Any = None
        self.constant: float | None = None

    def _fit(self, x: np.ndarray, y: np.ndarray) -> None:
        import lightgbm as lgb

        params = dict(LOCKED_LGBM_PARAMS_V1)
        params["seed"] = int(self.hyperparameters["seed"])
        if self.task == "classification":
            if np.unique(y).size < 2:
                self.constant = float(np.unique(y)[0])
                self.model_text = ""
                return
            params["objective"] = "binary"
        else:
            params["objective"] = "regression"
        n_estimators = int(params.pop("n_estimators"))
        booster = lgb.train(params, lgb.Dataset(x, label=y), num_boost_round=n_estimators)
        self._booster = booster
        self.model_text = booster.model_to_string()
        self.constant = None

    def _predict(self, x: np.ndarray) -> np.ndarray:
        if self.constant is not None:
            return np.full(x.shape[0], 0.5 + 0.49 * (1.0 if self.constant > 0 else -1.0))
        if self._booster is None:
            import lightgbm as lgb

            self._booster = lgb.Booster(model_str=self.model_text)
        return np.asarray(self._booster.predict(x), dtype=float)

    def _state(self) -> dict[str, Any]:
        return {"model_text": self.model_text, "constant": self.constant, "params": LOCKED_LGBM_PARAMS_V1}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.model_text = str(state["model_text"])
        self.constant = state.get("constant")
        self._booster = None
        if state.get("params") != LOCKED_LGBM_PARAMS_V1:
            raise ValueError("paramètres LightGBM différents de la version verrouillée")


MODEL_FAMILIES: dict[str, type[BaseModel]] = {
    FlatNoTradeBenchmark.family: FlatNoTradeBenchmark,
    RidgeModel.family: RidgeModel,
    LogisticModel.family: LogisticModel,
    SingleFeatureSlopeModel.family: SingleFeatureSlopeModel,
    MomentumBaseline.family: MomentumBaseline,
    MeanReversionBaseline.family: MeanReversionBaseline,
    LockedLightGBM.family: LockedLightGBM,
}


def candidate_grid(candidate: str, *, seed: int, feature_names: list[str]) -> list[BaseModel]:
    """Grille d'essais par candidat (chaque essai est enregistré, pas seulement le gagnant)."""
    if candidate == "flat":
        return [FlatNoTradeBenchmark()]
    if candidate == "ridge":
        return [RidgeModel(alpha=a) for a in (0.1, 1.0, 10.0, 100.0)]
    if candidate == "logistic":
        return [LogisticModel(c=c) for c in (0.01, 0.1, 1.0)]
    if candidate == "momentum":
        idx = _feature_index(feature_names, ("momentum_30m_skip_5m", "ret_15m", "ret_5m"))
        return [MomentumBaseline(feature_index=idx, sign=1)]
    if candidate == "mean_reversion":
        idx = _feature_index(feature_names, ("reversal_z_15m", "vwap_dist_30m", "ret_1m"))
        return [MeanReversionBaseline(feature_index=idx, sign=-1)]
    if candidate == "lightgbm":
        return [LockedLightGBM(task="regression", seed=seed)]
    if candidate == "lightgbm_classifier":
        return [LockedLightGBM(task="classification", seed=seed)]
    raise ValueError(f"candidat inconnu : {candidate}")


def _feature_index(names: list[str], preferred: tuple[str, ...]) -> int:
    for p in preferred:
        if p in names:
            return names.index(p)
    raise ValueError(f"aucune des features {preferred} n'est disponible pour cette baseline")


def information_coefficient(pred: np.ndarray, y: np.ndarray) -> float:
    """Corrélation de Pearson prédiction/cible (0 si variance nulle)."""
    if pred.size < 3 or float(np.std(pred)) == 0 or float(np.std(y)) == 0:
        return 0.0
    c = float(np.corrcoef(pred, y)[0, 1])
    return c if math.isfinite(c) else 0.0

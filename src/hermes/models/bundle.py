"""Serialisable model bundle: everything the live engine needs to score, nothing it could misuse.

No pickle: LightGBM boosters are stored in their native text format, linear models as ``.npz`` arrays,
metadata as JSON. A bundle records the exact feature list and configuration it was trained with, the data
window, the causal prior IC, and the evaluation verdict (``promoted``). The live engine refuses to trade
real money with a bundle whose ``promoted`` flag is false.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np

from hermes.config import HermesConfig, LinearConfig
from hermes.models.base import cs_standardize
from hermes.models.gbm import GBMModel
from hermes.models.linear import RidgeModel


@dataclass
class ModelBundle:
    config: HermesConfig
    feature_names: list[str]
    gbm: GBMModel | None
    ridge: RidgeModel | None
    weights: dict[str, float]
    prior_ic: float
    market: RidgeModel | None = None
    market_feature_names: list[str] = field(default_factory=list)
    market_prior_ic: float = 0.0
    meta: dict[str, object] = field(default_factory=dict)

    # -- scoring -------------------------------------------------------------------------------------------
    def score(self, X: np.ndarray, groups: np.ndarray) -> np.ndarray:
        preds = {}
        if self.gbm is not None and self.weights.get("gbm", 0) > 0:
            preds["gbm"] = cs_standardize(self.gbm.predict(X), groups)
        if self.ridge is not None and self.weights.get("ridge", 0) > 0:
            preds["ridge"] = cs_standardize(self.ridge.predict(X), groups)
        if not preds:
            return np.full(len(X), np.nan)
        combo = sum(self.weights[k] * np.nan_to_num(p) for k, p in preds.items())
        return cs_standardize(combo, groups)

    def market_score(self, x: np.ndarray) -> float:
        if self.market is None:
            return float("nan")
        return float(self.market.predict(np.nan_to_num(x.reshape(1, -1)))[0])

    @property
    def promoted(self) -> bool:
        return bool(self.meta.get("promoted", False))

    # -- persistence -----------------------------------------------------------------------------------------
    def save(self, directory: str | Path) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        if self.gbm is not None:
            for i, b in enumerate(self.gbm.boosters):
                name = f"gbm_{i}.txt"
                (d / name).write_text(b.model_to_string())
                files[name] = ""
        for tag, m in (("ridge", self.ridge), ("market", self.market)):
            if m is not None:
                np.savez(d / f"{tag}.npz", mu=m.mu, sd=m.sd, coef=m.coef)
                files[f"{tag}.npz"] = ""
        for name in files:
            files[name] = hashlib.sha256((d / name).read_bytes()).hexdigest()
        meta = {
            "created_at": datetime.now(UTC).isoformat(),
            "feature_names": self.feature_names,
            "market_feature_names": self.market_feature_names,
            "weights": self.weights,
            "prior_ic": self.prior_ic,
            "market_prior_ic": self.market_prior_ic,
            "files": files,
            **self.meta,
        }
        (d / "bundle.json").write_text(json.dumps(meta, indent=2, default=str))
        (d / "config.json").write_text(self.config.model_dump_json(indent=2))
        return d

    @classmethod
    def load(cls, directory: str | Path) -> ModelBundle:
        d = Path(directory)
        meta = json.loads((d / "bundle.json").read_text())
        for name, digest in meta.get("files", {}).items():
            if hashlib.sha256((d / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"bundle file {name} does not match its recorded hash")
        cfg = HermesConfig.model_validate_json((d / "config.json").read_text())
        gbm = None
        boosters = sorted(d.glob("gbm_*.txt"))
        if boosters:
            gbm = GBMModel(cfg.model.gbm)
            gbm.boosters = [lgb.Booster(model_str=p.read_text()) for p in boosters]

        def _ridge(tag: str, alpha: float) -> RidgeModel | None:
            p = d / f"{tag}.npz"
            if not p.exists():
                return None
            z = np.load(p)
            m = RidgeModel(LinearConfig(alpha=alpha))
            m.mu, m.sd, m.coef = z["mu"], z["sd"], z["coef"]
            return m

        extra = {
            k: v
            for k, v in meta.items()
            if k not in ("feature_names", "market_feature_names", "weights", "prior_ic", "market_prior_ic", "files")
        }
        return cls(
            config=cfg,
            feature_names=list(meta["feature_names"]),
            gbm=gbm,
            ridge=_ridge("ridge", cfg.model.linear.alpha),
            weights={k: float(v) for k, v in meta["weights"].items()},
            prior_ic=float(meta["prior_ic"]),
            market=_ridge("market", 3000.0),
            market_feature_names=list(meta.get("market_feature_names", [])),
            market_prior_ic=float(meta.get("market_prior_ic", 0.0)),
            meta=extra,
        )

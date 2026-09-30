"""Serialisable model bundle: everything the live engine needs to score, nothing it could misuse.

No pickle: LightGBM boosters are stored in their native text format, linear models as ``.npz`` arrays,
metadata as JSON. A bundle records the exact feature list and configuration it was trained with, the data
window, the causal prior IC, and the evaluation verdict (``promoted``). The live engine refuses to trade
real money with a bundle whose ``promoted`` flag is false.

Format 2 hashes ``config.json`` together with the models. The manifest is authoritative: extra files in
the directory are never loaded, including models left by an earlier save. Hashes detect accidental changes;
they are not signatures and cannot authenticate a manifest rewritten together with its payloads.

Legacy bundles with a model manifest but no configuration hash still load with a RuntimeWarning, preserving
their existing promotion status so deployed installations can migrate deliberately. Their historical config
integrity cannot be established. Verify their configuration against the evaluated run (or reevaluate), then
save to a new directory to migrate; re-saving alone does not prove that the legacy strategy was evaluated.
Publish using ``hermes model install`` for an atomic directory swap; saving over a live directory can expose
a temporarily inconsistent bundle, which loading rejects.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np

from hermes.config import HermesConfig, LinearConfig
from hermes.models.base import cs_standardize
from hermes.models.gbm import GBMModel
from hermes.models.linear import RidgeModel

_FORMAT_VERSION = 2
_BOOSTER_NAME = re.compile(r"gbm_(0|[1-9][0-9]*)\.txt")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _verified_files(directory: Path, meta: dict) -> dict[str, bytes]:
    """Read each declared payload once; deserializers must consume these same verified bytes."""
    version = meta.get("format_version", 1)
    if type(version) is not int or version not in (1, _FORMAT_VERSION):
        raise ValueError(f"unsupported bundle format version: {version!r}")
    files = meta.get("files")
    if not isinstance(files, dict):
        raise ValueError("bundle must contain a files manifest")
    if version == _FORMAT_VERSION and "config.json" not in files:
        raise ValueError("bundle format 2 requires config.json in the files manifest")
    payloads = {}
    for name, digest in files.items():
        if name not in {"config.json", "ridge.npz", "market.npz"} and not _BOOSTER_NAME.fullmatch(name):
            raise ValueError(f"unsupported bundle file name: {name!r}")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError(f"bundle file {name} has an invalid SHA-256 digest")
        try:
            payload = (directory / name).read_bytes()
        except OSError as exc:
            raise ValueError(f"bundle file {name} cannot be read") from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"bundle file {name} does not match its recorded hash")
        payloads[name] = payload
    indices = sorted(int(match[1]) for name in files if (match := _BOOSTER_NAME.fullmatch(name)))
    if indices != list(range(len(indices))):
        raise ValueError("bundle booster manifest must contain consecutive indices starting at zero")
    if "config.json" not in files:
        warnings.warn(
            "Legacy bundle has no config.json integrity hash; verify against the evaluated configuration "
            "or reevaluate before re-saving in format 2. Existing promotion status is preserved.",
            RuntimeWarning,
            stacklevel=3,
        )
        payloads["config.json"] = (directory / "config.json").read_bytes()
    return payloads


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
        X = np.asarray(X, dtype=np.float32)
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
        (d / "config.json").write_text(self.config.model_dump_json(indent=2))
        files["config.json"] = ""
        for name in files:
            files[name] = hashlib.sha256((d / name).read_bytes()).hexdigest()
        meta = {
            **self.meta,
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "feature_names": self.feature_names,
            "market_feature_names": self.market_feature_names,
            "weights": self.weights,
            "prior_ic": self.prior_ic,
            "market_prior_ic": self.market_prior_ic,
            "files": files,
        }
        # Publish the manifest after every referenced payload has been written.
        (d / "bundle.json").write_text(json.dumps(meta, indent=2, default=str))
        return d

    @classmethod
    def load(cls, directory: str | Path) -> ModelBundle:
        d = Path(directory)
        meta = json.loads((d / "bundle.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError("bundle metadata must be a JSON object")
        payloads = _verified_files(d, meta)
        cfg = HermesConfig.model_validate_json(payloads["config.json"])
        weights = {k: float(v) for k, v in meta["weights"].items()}
        gbm = None
        boosters = sorted(
            (name for name in payloads if _BOOSTER_NAME.fullmatch(name)),
            key=lambda name: int(name[4:-4]),
        )
        for tag, present in (("gbm", bool(boosters)), ("ridge", "ridge.npz" in payloads)):
            if weights.get(tag, 0) > 0 and not present:
                raise ValueError(f"bundle gives positive weight to {tag} without a manifested model")
        if boosters:
            gbm = GBMModel(cfg.model.gbm)
            gbm.boosters = [lgb.Booster(model_str=payloads[name].decode()) for name in boosters]

        def _ridge(tag: str, alpha: float) -> RidgeModel | None:
            name = f"{tag}.npz"
            if name not in payloads:
                return None
            m = RidgeModel(LinearConfig(alpha=alpha))
            with np.load(io.BytesIO(payloads[name]), allow_pickle=False) as z:
                m.mu, m.sd, m.coef = z["mu"], z["sd"], z["coef"]
            return m

        extra = {
            k: v
            for k, v in meta.items()
            if k
            not in (
                "format_version",
                "feature_names",
                "market_feature_names",
                "weights",
                "prior_ic",
                "market_prior_ic",
                "files",
            )
        }
        return cls(
            config=cfg,
            feature_names=list(meta["feature_names"]),
            gbm=gbm,
            ridge=_ridge("ridge", cfg.model.linear.alpha),
            weights=weights,
            prior_ic=float(meta["prior_ic"]),
            market=_ridge("market", 3000.0),
            market_feature_names=list(meta.get("market_feature_names", [])),
            market_prior_ic=float(meta.get("market_prior_ic", 0.0)),
            meta=extra,
        )

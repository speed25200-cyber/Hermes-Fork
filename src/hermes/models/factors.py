"""Six fixed economic challengers expressed through the existing Ridge bundle.

No coefficient is fitted. Each primitive is rounded to float16, converted back to float32 and passed
through Ridge's fixed scaling, missing-value handling and clipping. The combined rule averages the
five *raw* rules, before ModelBundle performs its usual cross-sectional standardisation. It is not an
average of separately standardised scores. Research and serving must both call the bundle to score.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd

from hermes.config import HermesConfig
from hermes.features.library import STORAGE_DTYPE, FeatureSet
from hermes.models.bundle import ModelBundle
from hermes.models.linear import RidgeModel


@dataclass(frozen=True)
class FactorTerm:
    feature: str
    coefficient: float
    scale: float = 1.0


@dataclass(frozen=True)
class FactorRule:
    name: str
    definition: str
    terms: tuple[FactorTerm, ...]


_PRIMITIVE_RULES = (
    FactorRule(
        "carry",
        "-Z(cs_funding_sum_10080m): buy low settled seven-day funding and sell high funding",
        (FactorTerm("cs_funding_sum_10080m", -1.0),),
    ),
    FactorRule(
        "basis",
        "-Z(cs_premium): buy discounted perpetuals and sell expensive perpetuals",
        (FactorTerm("cs_premium", -1.0),),
    ),
    FactorRule(
        "reversal",
        "-0.5*Z(iret_240m) - 0.5*Z(iret_1440m): fade four-hour and one-day residual moves",
        (FactorTerm("iret_240m", -0.5), FactorTerm("iret_1440m", -0.5)),
    ),
    FactorRule(
        "momentum",
        "Z(iret_10080m) - sqrt(1/7)*Z(iret_1440m): seven-day residual momentum excluding the last day",
        (FactorTerm("iret_10080m", 1.0), FactorTerm("iret_1440m", -sqrt(1.0 / 7.0))),
    ),
    FactorRule(
        "flow",
        "Z(flow_ret_div; scale=0.1): follow relative aggressive flow not yet reflected in residual price",
        (FactorTerm("flow_ret_div", 1.0, 0.1),),
    ),
)


def _combined_rule() -> FactorRule:
    coefficients: dict[tuple[str, float], float] = {}
    for rule in _PRIMITIVE_RULES:
        for term in rule.terms:
            key = term.feature, term.scale
            coefficients[key] = coefficients.get(key, 0.0) + term.coefficient / len(_PRIMITIVE_RULES)
    return FactorRule(
        "combined",
        "Arithmetic mean of the five raw fixed rules; cross-sectional standardisation follows the sum",
        tuple(FactorTerm(name, coefficient, scale) for (name, scale), coefficient in coefficients.items()),
    )


FACTOR_RULES = (*_PRIMITIVE_RULES, _combined_rule())
FACTOR_NAMES = tuple(rule.name for rule in FACTOR_RULES)


def build_factor_bundle(cfg: HermesConfig, name: str) -> ModelBundle:
    """Create an unpromoted deterministic challenger with a zero IC prior.

    The supplied configuration determines features, costs, risk and the book; callers freeze that common
    configuration before evaluation. Only the scoring model is replaced. A live consumer must select the
    bundle's required feature columns in their recorded order and reject missing columns.
    """
    rules = {rule.name: rule for rule in FACTOR_RULES}
    if name not in rules:
        raise ValueError(f"Unknown fixed factor {name!r}; expected one of {FACTOR_NAMES}")
    rule = rules[name]
    model = RidgeModel(cfg.model.linear)
    model.mu = np.zeros(len(rule.terms), dtype=np.float64)
    model.sd = np.array([term.scale for term in rule.terms], dtype=np.float64)
    model.coef = np.array([term.coefficient for term in rule.terms], dtype=np.float64)
    return ModelBundle(
        config=cfg.model_copy(deep=True),
        feature_names=[term.feature for term in rule.terms],
        gbm=None,
        ridge=model,
        weights={"ridge": 1.0},
        prior_ic=0.0,
        meta={
            "model_kind": "deterministic_factor",
            "factor_rule": name,
            "factor_definition": rule.definition,
            "factor_transform": (
                "Z(x; scale) = clip(nan_to_num(float32(float16(x))/scale, nan=0, posinf=0, neginf=0), -5, 5)"
            ),
            "factor_terms": [
                {"feature": term.feature, "coefficient": term.coefficient, "scale": term.scale} for term in rule.terms
            ],
            "coefficient_source": "fixed_before_evaluation",
            "fitted": False,
            "promoted": False,
            "market_promoted": False,
        },
    )


def score_factor_bundle(bundle: ModelBundle, feats: FeatureSet, mask: pd.DataFrame) -> pd.DataFrame:
    """Score member rows through exactly the persisted bundle, with serving precision.

    Absent feature columns are configuration/data errors. Individual missing cells follow the existing
    Ridge neutral-value convention; no alternate formula or fitted imputer is used here.
    """
    if not bundle.feature_names or len(set(bundle.feature_names)) != len(bundle.feature_names):
        raise ValueError("Factor feature names must be nonempty and unique")
    missing = sorted(set(bundle.feature_names) - set(feats.frames))
    if missing:
        raise ValueError(f"Missing factor features: {missing}")
    selected = FeatureSet(frames={name: feats.frames[name] for name in bundle.feature_names})
    X, mi = selected.stack(mask, dtype=STORAGE_DTYPE)
    t_pos = mask.index.get_indexer(mi.get_level_values(0))
    s_pos = mask.columns.get_indexer(mi.get_level_values(1))
    values = bundle.score(X.astype(np.float32), t_pos)
    out = np.full(mask.shape, np.nan, dtype=np.float64)
    out[t_pos, s_pos] = values
    return pd.DataFrame(out, index=mask.index, columns=mask.columns)


def score_factors(feats: FeatureSet, mask: pd.DataFrame, cfg: HermesConfig) -> dict[str, pd.DataFrame]:
    """Score the six predefined challengers; never select one or modify its coefficients."""
    return {name: score_factor_bundle(build_factor_bundle(cfg, name), feats, mask) for name in FACTOR_NAMES}

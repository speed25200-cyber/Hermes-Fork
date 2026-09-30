"""Registered, chronological research for the six fixed economic factor rules.

This runner fits nothing, never promotes a model and never starts a trading process. It registers all
six attempts before any simulation and persists the same zero-prior bundles used to generate scores.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.backtest.engine import run_backtest
from hermes.config import HermesConfig
from hermes.data.panel import Panel
from hermes.data.universe import universe_mask
from hermes.features.library import FeatureSet, build_features, feature_warmup_bars
from hermes.labels.targets import build_targets
from hermes.models.factors import FACTOR_NAMES, FACTOR_RULES, build_factor_bundle, score_factor_bundle
from hermes.research.dataset import Dataset
from hermes.research.evaluate import make_signal
from hermes.research.provenance import array_sha256, file_sha256, numerical_versions
from hermes.research.selection import SelectionProtocol, evaluate_candidates

log = logging.getLogger(__name__)

_LIMITS = [
    "Research is restricted to the supplied candidate pool; point-in-time selection cannot restore omitted contracts.",
    "Previously examined market dates remain retrospective evidence, even though these rules are now fixed.",
    "Binance prices and settled funding proxy the OKX market; modeled costs are not observed OKX fills.",
    "All candidates start flat at calibration start and run continuously across the three windows.",
    "Window returns describe slices of continuous accounts, not accounts restarted at each boundary.",
    "The best calibration candidate is an exploratory shadow candidate, not a promotion or trading authorization.",
    "All six bundles stay unpromoted, including synthetic-data runs and positive historical results.",
    "No cash yield, future profitability or independence of already examined history is assumed.",
]


def _clean(value):
    if isinstance(value, (pd.DataFrame, pd.Series)):
        return {
            "kind": type(value).__name__,
            "shape": list(value.shape),
            "columns": list(map(str, value.columns)) if isinstance(value, pd.DataFrame) else [str(value.name)],
            "pandas_content_sha256": hashlib.sha256(
                pd.util.hash_pandas_object(value, index=True).to_numpy().tobytes()
            ).hexdigest(),
        }
    if isinstance(value, np.ndarray):
        return {"kind": "ndarray", "shape": list(value.shape), "sha256": array_sha256(value)}
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (Path, pd.Timestamp)):
        return str(value)
    return value


def _encoded(value) -> bytes:
    return json.dumps(
        _clean(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    """Atomically publish a complete new file, failing rather than replacing existing evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def _write_json(path: Path, value) -> None:
    _write_new(
        path, json.dumps(_clean(value), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    )


def _sources() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        "config.py",
        "data",
        "features",
        "labels",
        "models",
        "portfolio",
        "risk",
        "backtest",
        "validation",
        "research/dataset.py",
        "research/evaluate.py",
        "research/factor_research.py",
        "research/provenance.py",
        "research/selection.py",
    )
    files = []
    for relative in paths:
        path = root / relative
        files.extend(path.rglob("*.py") if path.is_dir() else [path])
    return {str(path.relative_to(root)): file_sha256(path) for path in sorted(files)}


def _normalise_panel(panel: Panel) -> Panel:
    index = panel.index.tz_convert("UTC").as_unit("ns")
    fields = {}
    for name, frame in panel.fields.items():
        copy = frame.copy(deep=False)
        copy.index = index
        fields[name] = copy
    return Panel(fields, bar=panel.bar, meta=dict(panel.meta))


def _panel_manifest(panel: Panel) -> dict:
    body = {
        "bar": panel.bar,
        "start": str(panel.index[0]),
        "end": str(panel.index[-1]),
        "symbols": panel.symbols,
        "meta": panel.meta,
        "index_sha256": array_sha256(panel.index.asi8),
        "fields": {name: array_sha256(frame.to_numpy()) for name, frame in sorted(panel.fields.items())},
    }
    return {**body, "sha256": _digest(body)}


def _frame_manifest(frame: pd.DataFrame | pd.Series) -> dict:
    if isinstance(frame, pd.Series):
        frame = frame.to_frame()
    body = {
        "index_sha256": array_sha256(frame.index.as_unit("ns").asi8),
        "columns": list(map(str, frame.columns)),
        "values_sha256": array_sha256(frame.to_numpy()),
    }
    return {**body, "sha256": _digest(body)}


def _dataset(panel: Panel, mask: pd.DataFrame, feats: FeatureSet, cfg: HermesConfig) -> Dataset:
    """Evaluation needs exact risk auxiliaries and targets, but no long training matrix."""
    targets = build_targets(panel, feats, mask, cfg.labels)
    return Dataset(
        panel=panel,
        mask=mask,
        feats=feats,
        targets=targets,
        X=np.empty((0, 0), dtype=np.float16),
        t_pos=np.empty(0, dtype=np.int32),
        s_pos=np.empty(0, dtype=np.int32),
        y=np.empty(0, dtype=np.float32),
        y_raw=np.empty(0, dtype=np.float32),
        feature_names=feats.names,
        market_X=np.empty((len(panel.index), 0), dtype=np.float32),
        market_y=np.empty(0, dtype=np.float32),
        market_names=[],
    )


def _daily(returns: pd.Series, expected: pd.DatetimeIndex) -> pd.Series:
    actual = returns.copy(deep=False)
    actual.index = actual.index.tz_convert("UTC").as_unit("ns")
    if not actual.index.equals(expected):
        raise ValueError("Backtest returns do not cover every declared simulation bar exactly")
    values = actual.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < -1).any():
        raise ValueError("Backtest returns contain invalid values")
    return (1.0 + actual).groupby(actual.index.floor("D")).prod() - 1.0


def _report_markdown(report: dict) -> str:
    evaluation = report["selection"]

    def pct(value):
        return "indisponible" if value is None else f"{100 * value:+.3f} %"

    lines = [
        "# Recherche des six facteurs figés",
        "",
        f"Décision historique : **{evaluation['selected']}**. "
        f"Meilleur candidat de calibration : **{evaluation['best_calibration_candidate']}**.",
        "Aucun modèle n'est promu et aucun processus de trading n'est activé.",
        "",
        "| Candidat | Calibration net | Confirmation net | Test net | Test coûts ×2 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in FACTOR_NAMES:
        candidate = evaluation["candidates"][name]
        values = [pct(candidate[part]["net_return"]) for part in ("calibration", "confirmation", "test")]
        stress = candidate["test"]["stress"]
        lines.append(f"| {name} | " + " | ".join([*values, pct(stress["net_return"])]) + " |")
    lines += [
        "",
        "Le choix utilise uniquement la borne inférieure de moyenne en calibration, puis son rendement net. "
        "La confirmation et le test ne choisissent jamais de remplaçant. Le cash reste le choix si aucun "
        "candidat ne franchit la règle déclarée.",
        "",
        f"Verdict : `{evaluation['holdout_verdict']}`. "
        f"Multiplicité déclarée : {evaluation['multiplicity_trials']} essais. "
        f"Données synthétiques : {report['synthetic']}.",
        "",
        "Les comptes partent sans position au début de la calibration et restent continus entre les fenêtres. "
        "Frais, spread, impact, funding et stops suivent le moteur partagé; le stress double les coûts payés. "
        "Les données Binance ne prouvent pas les résultats de fills réels sur OKX.",
        "",
        "Les dates et le bassin de candidats ont déjà été examinés. Ces chiffres sont rétrospectifs; "
        "ils ne démontrent pas la rentabilité future. Le pointeur shadow reste exploratoire même si "
        "la sélection économique conclut au cash.",
        "",
        "`declaration.json` et les six entrées `trials/` précèdent toutes les simulations. "
        "`report.json` contient les empreintes de données, code, versions, scores et IC. "
        "Les CSV quotidiens sont dans `daily/`; les séries volumineuses sont ignorées par Git dans `local/`.",
        "",
    ]
    return "\n".join(lines)


def run_factor_research(
    cfg: HermesConfig,
    panel: Panel,
    out: str | Path,
    protocol: SelectionProtocol,
    capital: float = 10_000.0,
    ledger: str | Path | None = None,
) -> dict:
    """Run the declared family once, return the report, and refuse to overwrite evidence.

    ``ledger`` optionally mirrors the six pre-registered trial records into the repository ledger.
    Irrespective of that option, six local trial records are created before the first simulation.
    """
    destination = Path(out)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Factor research output must be a new or empty directory")
    if not np.isfinite(capital) or capital <= 0:
        raise ValueError("capital must be positive and finite")
    if cfg.portfolio.books:
        raise ValueError("Factor research requires the declared single book, not an ensemble of book settings")
    if cfg.portfolio.holding_horizon not in cfg.labels.horizons:
        raise ValueError("The holding horizon must have an exact declared target")
    if panel.bar != cfg.data.bar or not len(panel.index):
        raise ValueError("Panel timeframe must match the configuration and the panel must be nonempty")
    if protocol.family_max != len(FACTOR_NAMES):
        raise ValueError("The factor protocol must reserve all six candidate trials")
    if not cfg.costs.include_funding or "funding_rate" not in panel:
        raise ValueError("Factor research requires settled funding in both data and costs")
    synthetic = cfg.data.source == "synthetic" or bool(panel.meta.get("synthetic"))
    if cfg.data.universe.venue == "okx" and not synthetic and "venue_listed" not in panel:
        raise ValueError("An OKX research universe requires the historical venue listing field")
    panel = _normalise_panel(panel)
    start = pd.Timestamp(protocol.calibration.start, tz="UTC")
    end = pd.Timestamp(protocol.test.end, tz="UTC") + pd.Timedelta(days=1) - panel.bar_delta
    expected = pd.date_range(start, end, freq=panel.bar_delta).as_unit("ns")
    observed = panel.index[(panel.index >= start) & (panel.index <= end)]
    if not observed.equals(expected):
        raise ValueError("Panel does not cover every bar of the declared calendar windows")

    config = json.loads(cfg.model_dump_json())
    rules = [asdict(rule) for rule in FACTOR_RULES]
    protocol_dict = asdict(protocol)
    # Match selection.py's published protocol hash exactly.
    protocol_hash = hashlib.sha256(json.dumps(protocol_dict, sort_keys=True).encode()).hexdigest()
    sources = _sources()
    declaration = {
        "schema_version": 1,
        "registered_at": datetime.now(UTC).isoformat(),
        "kind": "fixed_factor_family",
        "config": config,
        "config_sha256": _digest(config),
        "protocol": protocol_dict,
        "protocol_sha256": protocol_hash,
        "rules": rules,
        "rule_transform": "Existing Ridge preprocessing, float16-to-float32 inputs, then ModelBundle.score",
        "capital": float(capital),
        "simulation_start": str(start),
        "simulation_end": str(end),
        "account_convention": "flat_start_then_continuous_across_windows",
        "cost_multipliers": [1.0, 2.0],
        "prior_ic": 0.0,
        "panel": _panel_manifest(panel),
        "sources": sources,
        "versions": numerical_versions(),
        "synthetic": synthetic,
        "promotion_eligible": False,
        "limits": _LIMITS,
    }
    declaration["sha256"] = _digest(declaration)
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "declaration.json", declaration)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    factor_config_hashes = {}
    for rule in rules:
        factor_config_hashes[rule["name"]] = _digest({"config": config, "rule": rule})
        record = {
            "registered_at": declaration["registered_at"],
            "type": "factor",
            "status": "registered_before_simulation",
            "config_hash": factor_config_hashes[rule["name"]],
            "common_config_sha256": declaration["config_sha256"],
            "model_rule": rule,
            "protocol_sha256": protocol_hash,
            "declaration_sha256": declaration["sha256"],
            "panel_sha256": declaration["panel"]["sha256"],
            "promotion_eligible": False,
        }
        filename = f"{stamp}-{declaration['sha256'][:12]}-{rule['name']}.json"
        _write_json(destination / "trials" / filename, record)
        if ledger is not None and Path(ledger).resolve() != (destination / "trials").resolve():
            _write_json(Path(ledger) / filename, record)
    _write_new(destination / "local" / ".gitignore", b"*\n!.gitignore\n")
    log.info("Registered six fixed factor rules before simulation: %s", destination / "declaration.json")

    mask = universe_mask(panel, cfg.data.universe)
    # Keep the exact common universe while avoiding feature matrices for never-active contracts.
    keep = list(mask.columns[mask.any(axis=0)])
    if "BTCUSDT" in panel.symbols and "BTCUSDT" not in keep:
        keep.append("BTCUSDT")
    if not keep:
        raise ValueError("No eligible contracts in the supplied panel")
    panel = panel.subset(keep)
    mask = mask[keep]
    feats = build_features(panel, mask, cfg.features)
    ds = _dataset(panel, mask, feats, cfg)
    bundles = {name: build_factor_bundle(cfg, name) for name in FACTOR_NAMES}
    scores = {name: score_factor_bundle(bundle, feats, mask) for name, bundle in bundles.items()}
    # Features have served their only purpose. Keep exact risk auxiliaries and targets for evaluation.
    ds.feats = FeatureSet(frames={}, aux=feats.aux)
    del feats
    prior = pd.Series(0.0, index=panel.index)
    signals = {name: make_signal(score, ds, prior, cfg) for name, score in scores.items()}
    inputs = {
        "retained_symbols": keep,
        "universe": _frame_manifest(mask),
        "warmup_bars_available": int(panel.index.searchsorted(start)),
        "feature_warmup_bars": feature_warmup_bars(cfg),
        "scores": {name: _frame_manifest(score) for name, score in scores.items()},
        "traded_scores": {name: _frame_manifest(signal.score) for name, signal in signals.items()},
        "ic": {name: _frame_manifest(signal.ic_est) for name, signal in signals.items()},
        "cost_scale": {name: _frame_manifest(signal.cost_scale) for name, signal in signals.items()},
    }
    inputs["sha256"] = _digest(inputs)
    _write_json(destination / "inputs.json", inputs)
    for name, bundle in bundles.items():
        bundle.meta.update(
            config_hash=factor_config_hashes[name],
            factor_declaration_sha256=declaration["sha256"],
            factor_inputs_sha256=inputs["sha256"],
            factor_protocol_sha256=protocol_hash,
            synthetic=synthetic,
        )
        bundle.save(destination / "models" / name)
        scores[name].to_parquet(destination / "local" / f"{name}-scores.parquet")
        pd.DataFrame({"ic": signals[name].ic_est, "cost_scale": signals[name].cost_scale}).to_parquet(
            destination / "local" / f"{name}-signals.parquet"
        )
    if _sources() != sources:
        raise RuntimeError("Research sources changed after declaration; use a new frozen run directory")

    base_daily, stress_daily, summaries = {}, {}, {}
    for name in FACTOR_NAMES:
        summaries[name] = {}
        for multiplier in (1.0, 2.0):
            log.info("Running registered factor %s with costs x%.0f", name, multiplier)
            result = run_backtest(
                panel,
                mask,
                ds.feats.aux,
                signals[name],
                cfg,
                capital=capital,
                start=start,
                end=end,
                cost_multiplier=multiplier,
            )
            daily = _daily(result.returns, expected)
            label = "base" if multiplier == 1.0 else "cost2"
            stem = name if multiplier == 1.0 else f"{name}_cost2"
            (base_daily if multiplier == 1.0 else stress_daily)[name] = daily
            _write_new(destination / "daily" / f"{stem}.csv", daily.rename("net_return").to_csv().encode())
            result.stats.to_parquet(destination / "local" / f"{stem}-stats.parquet")
            pd.DataFrame({"return": result.returns, "equity": result.equity}).to_parquet(
                destination / "local" / f"{stem}-returns.parquet"
            )
            summaries[name][label] = {
                "metrics": result.summary(cfg.bars_per_year),
                "bar_returns": _frame_manifest(result.returns),
                "daily_returns": _frame_manifest(daily),
                "risk_events": result.risk_events,
            }
    if _sources() != sources:
        raise RuntimeError("Research sources changed during simulation; results cannot attest the declared code")
    evaluation = evaluate_candidates(
        pd.DataFrame(base_daily), protocol, stressed_daily_returns=pd.DataFrame(stress_daily)
    )
    report = {
        "schema_version": 1,
        "declaration_sha256": declaration["sha256"],
        "config_sha256": declaration["config_sha256"],
        "panel_sha256": declaration["panel"]["sha256"],
        "sources": sources,
        "versions": declaration["versions"],
        "inputs": inputs,
        "synthetic": synthetic,
        "promotion_eligible": False,
        "selection": evaluation,
        "summaries": summaries,
        "limits": _LIMITS,
    }
    report = _clean(report)
    _write_json(destination / "report.json", report)
    best = evaluation["best_calibration_candidate"]
    _write_json(
        destination / "shadow_best.json",
        {
            "candidate": best,
            "bundle": None if best is None else f"models/{best}",
            "selection_basis": "calibration_only",
            "economic_selection": evaluation["selected"],
            "exploratory_only": True,
            "promoted": False,
            "activation": "none",
            "declaration_sha256": declaration["sha256"],
        },
    )
    _write_new(destination / "REPORT.md", _report_markdown(report).encode())
    return report

"""Diagnostic économique reproductible, sans entraînement ni modification du moteur.

Les périodes récentes restent des sous-périodes d'un historique déjà consulté : ce diagnostic
ne les transforme pas en nouveau test indépendant et ne change aucune porte de promotion.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.validation.stats import sharpe


def _read_json(path: Path) -> tuple[dict, dict[str, str]]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"objet JSON attendu : {path}")
    return data, _source(path, raw)


def _source(path: Path, raw: bytes) -> dict[str, str]:
    return {"file": path.name, "sha256": hashlib.sha256(raw).hexdigest()}


def _number(data: dict, key: str, *, positive: bool = False) -> float:
    value = data.get(key)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"valeur numérique absente : {key}")
    result = float(value)
    if not np.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"valeur numérique invalide : {key}")
    return result


def _window(daily: pd.DataFrame) -> dict:
    if daily.empty:
        return {"days": 0, "net_return": None, "max_drawdown": None, "sharpe": None}
    returns = daily["return"]
    equity = (1 + returns).cumprod()
    peak = equity.cummax().clip(lower=1.0)
    calendar_days = (daily.index[-1] - daily.index[0]).days + 1
    result = {
        "start": daily.index[0].isoformat(),
        "end": daily.index[-1].isoformat(),
        "days": len(daily),
        "missing_calendar_days": calendar_days - len(daily),
        "net_return": float(equity.iloc[-1] - 1),
        "max_drawdown": float((equity / peak - 1).min()),
        "sharpe": sharpe(returns.to_numpy(), 365.0) if len(daily) >= 30 and len(daily) == calendar_days else None,
    }
    for source, name in (("gross", "avg_gross"), ("ic_est", "avg_ic_est")):
        if source in daily:
            values = pd.to_numeric(daily[source], errors="raise").to_numpy(dtype=float)
            if not np.isfinite(values).all():
                raise ValueError(f"diagnostic non fini : {source}")
            result[name] = float(values.mean())
    return result


def audit_economics(report: Path, status: Path | None = None) -> dict:
    """Relit un rapport et ses rendements nets ; un instantané PAPER est facultatif.

    Aucun annualisé prospectif n'est extrapolé depuis un simple instantané. Pour DEMO/LIVE,
    les flux externes ne sont pas connus : la variation du solde n'est donc pas déclarée PnL.
    """
    report = report / "report.json" if report.is_dir() else report
    data, report_source = _read_json(report)
    daily_path = report.with_name("equity_daily.csv")
    daily_raw = daily_path.read_bytes()
    daily = pd.read_csv(io.BytesIO(daily_raw), index_col=0)
    if "return" not in daily or daily.empty:
        raise ValueError("historique de rendements nets absent")
    daily.index = pd.to_datetime(daily.index, utc=True)
    if daily.index.hasnans or daily.index.has_duplicates or not daily.index.is_monotonic_increasing:
        raise ValueError("dates des rendements invalides, dupliquées ou non ordonnées")
    if not daily.index.equals(daily.index.floor("D")):
        raise ValueError("le diagnostic exige une observation par jour UTC")
    returns = pd.to_numeric(daily["return"], errors="raise").to_numpy(dtype=float)
    if not np.isfinite(returns).all() or (returns < -1).any():
        raise ValueError("rendements nets non finis ou inférieurs à −100 %")
    daily["return"] = returns
    end = daily.index[-1]
    evaluation = data.get("evaluation", {})
    expected_return = evaluation.get("summary", {}).get("total_return")
    if expected_return is not None:
        expected = _number({"total_return": expected_return}, "total_return")
        actual = float(np.prod(1 + returns) - 1)
        # Le CSV historique est arrondi à six chiffres significatifs par write_report.
        tolerance = 1e-5 * max(1.0, abs(expected))
        if abs(actual - expected) > tolerance:
            raise ValueError("le rapport et le CSV ne décrivent pas le même rendement net")
    gate = evaluation.get("gate", {})
    limits = [
        "Historique déjà consulté : les fenêtres récentes ne sont pas un nouveau test indépendant.",
        "Les coûts et le funding historiques sont ceux du simulateur ; leur transfert vers OKX reste à mesurer.",
        "Ce diagnostic ne promeut aucun modèle et ne modifie ni positions, ni limites, ni seuils de validation.",
    ]
    result = {
        "schema_version": 1,
        "sources": {"report": report_source, "daily": _source(daily_path, daily_raw)},
        "config_hash": data.get("config_hash"),
        "historical": {
            "data_source": data.get("data", {}).get("source"),
            "generated_at": data.get("generated_at"),
            "promoted_at_evaluation": evaluation.get("promoted"),
            "gate_passed": sum(g.get("pass") is True for g in gate.values()),
            "gate_total": len(gate),
            "failed_criteria": [k for k, g in gate.items() if g.get("pass") is not True],
            "windows": {
                "all": _window(daily),
                "last_365_days": _window(daily.loc[daily.index > end - pd.Timedelta(days=365)]),
                "last_90_days": _window(daily.loc[daily.index > end - pd.Timedelta(days=90)]),
                "last_calendar_year_to_date": _window(daily.loc[daily.index.year == end.year]),
            },
        },
        "prospective": None,
        "limits": limits,
    }
    if status is None:
        limits.append("Aucun instantané prospectif fourni : rentabilité actuelle inconnue.")
        return result
    current, status_source = _read_json(status)
    result["sources"]["status"] = status_source
    bundle = current.get("bundle", {})
    expected_hash, actual_hash = data.get("config_hash"), bundle.get("config_hash")
    mode = current.get("mode")
    account = current.get("account", {})
    snapshot = {
        "observed_at": current.get("updated"),
        "bar": current.get("bar"),
        "mode": mode,
        "config_hash": actual_hash,
        "current_bundle_matches_report": bool(expected_hash and expected_hash == actual_hash),
        "promoted_in_status": bundle.get("promoted"),
        "ic_est": current.get("ic_est"),
        "ic_sizing": current.get("risk", {}).get("ic_sizing"),
        "paper_nominal_sizing": bool(
            mode == "paper"
            and current.get("risk", {}).get("ic_sizing", 0) > 0
            and current.get("ic_est") is not None
            and current["ic_est"] <= 0
        ),
        "net_pnl": None,
        "net_return": None,
        "assessment": "UNKNOWN",
    }
    if mode == "paper":
        initial = _number(account, "initial", positive=True)
        equity = _number(account, "equity")
        fees = _number(account, "fees_paid")
        funding_paid = _number(account, "funding_paid")
        pnl = equity - initial
        snapshot.update(
            initial_equity=initial,
            baseline_source="status.account.initial",
            pnl_assumption="unchanged_initial_capital_and_no_external_flows",
            equity=equity,
            net_pnl=pnl,
            net_return=pnl / initial,
            fees_paid=fees,
            funding_pnl=-funding_paid,
            price_and_execution_pnl=pnl + fees + funding_paid,
            assessment="LOSING_OBSERVED" if pnl < 0 else "POSITIVE_OBSERVED" if pnl > 0 else "FLAT_OBSERVED",
        )
        limits.append(
            "PAPER : frais et exécution simulés, absence de flux externes selon le contrat du PaperBroker. "
            "Le résidu prix/exécution inclut spread et glissement ; ce n'est pas un alpha brut."
        )
        limits.append(
            "Le capital initial publié vient de la configuration : le PnL suppose ce paramètre inchangé "
            "depuis la création du compte. La courbe persistante peut couvrir plusieurs modèles ; "
            "l'identité du modèle courant n'attribue pas le PnL cumulé à ce seul modèle."
        )
    else:
        limits.append("DEMO/LIVE : aucun PnL déduit du solde sans journal des dépôts et retraits.")
    if not snapshot["current_bundle_matches_report"]:
        limits.append("Le modèle de l'instantané ne correspond pas au rapport : comparaisons non attribuables.")
    if snapshot["paper_nominal_sizing"]:
        limits.append(
            "Le papier impose une taille nominale malgré l'IC estimé nul ou négatif. L'historique adapte la taille "
            "à l'IC : leurs rendements et expositions ne sont pas directement comparables."
        )
    limits.append(
        "Un instantané ne donne ni trajectoire quotidienne, ni durée d'observation, ni incertitude statistique ; "
        "un gain observé ne démontre pas une rentabilité future."
    )
    result["prospective"] = snapshot
    return result

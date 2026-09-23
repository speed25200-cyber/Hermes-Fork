"""Research report (JSON for machines, Markdown in French for people)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.backtest.engine import BacktestResult
from hermes.config import HermesConfig
from hermes.research.dataset import Dataset
from hermes.research.evaluate import Evaluation
from hermes.research.walkforward import WalkForwardResult


def _pct(x: float, nd: int = 1) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{100 * x:.{nd}f} %"


def _num(x: float, nd: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


GATE_LABELS = {
    "dsr": "Sharpe dégonflé (DSR) — probabilité que le vrai Sharpe > le meilleur hasard parmi les essais",
    "null_pvalue": "p-valeur exacte face au nul (mêmes scores permutés entre contrats par blocs d'une semaine)",
    "pbo": "Probabilité de sur-ajustement du backtest (PBO, CSCV sur la grille)",
    "sharpe": "Sharpe annualisé net de coûts (quotidien)",
    "positive_years": "Part des années civiles positives (années de moins de 90 jours exclues)",
    "oos_months": "Mois hors échantillon",
    "cost_stress": "Sharpe avec coûts doublés",
    "latency_stress": "Sharpe avec une barre de latence en plus",
}


def write_report(
    out: Path,
    cfg: HermesConfig,
    ds: Dataset,
    wf: WalkForwardResult,
    ev: Evaluation,
    bt: BacktestResult,
    config_hash: str,
    elapsed_s: float,
) -> None:
    daily = (1 + bt.returns).groupby(bt.returns.index.floor("D")).prod() - 1
    stats_daily = bt.stats.groupby(bt.stats.index.floor("D")).agg(
        {
            "gross": "mean",
            "net": "mean",
            "beta_exposure": "mean",
            "turnover": "sum",
            "fees": "sum",
            "spread": "sum",
            "impact": "sum",
            "funding": "sum",
            "gross_pnl": "sum",
            "pnl_long": "sum",
            "pnl_short": "sum",
            "n_positions": "mean",
            "stops": "sum",
            "ic_est": "mean",
        }
    )
    eq = pd.DataFrame({"return": daily, "equity": (1 + daily).cumprod()}).join(stats_daily)
    eq.to_csv(out / "equity_daily.csv", float_format="%.6g")
    if wf.feature_importance is not None:
        wf.feature_importance.to_csv(out / "feature_importance.csv", header=["gain_share"])
    (out / "folds.json").write_text(json.dumps(wf.folds, indent=1))
    meta = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config_hash": config_hash,
        "elapsed_seconds": round(elapsed_s),
        "data": {
            "source": cfg.data.source,
            "bar": cfg.data.bar,
            "start": str(ds.panel.index[0]),
            "end": str(ds.panel.index[-1]),
            "symbols": len(ds.panel.symbols),
            "avg_universe": float(ds.mask.sum(axis=1).mean()),
            "rows": len(ds.y),
            "features": len(ds.feature_names),
            "oos_start": str(wf.oof_start),
        },
        "evaluation": ev.to_dict(),
        "config": json.loads(cfg.model_dump_json()),
    }
    (out / "report.json").write_text(json.dumps(meta, indent=1, default=float))
    (out / "REPORT.md").write_text(render_markdown(meta, ev, wf), encoding="utf-8")


def render_markdown(meta: dict, ev: Evaluation, wf: WalkForwardResult) -> str:
    d = meta["data"]
    s = ev.summary
    t = ev.tests
    L: list[str] = []
    verdict = "✅ PROMU — autorisé à trader" if ev.promoted else "⛔ NON PROMU — interdit de capital réel"
    L += [
        "# Rapport de recherche Hermes",
        "",
        f"*Généré le {meta['generated_at']} · configuration `{meta['config_hash']}` · "
        f"{meta['elapsed_seconds']} s de calcul.*",
        "",
        f"## Verdict : {verdict}",
        "",
        "Toutes les mesures ci-dessous sont **hors échantillon** (walk-forward : chaque prédiction vient d'un "
        "modèle entraîné uniquement sur le passé) et **nettes de frais, spread, impact et funding**.",
        "",
        "| Porte | Valeur | Seuil | |",
        "|---|---:|---:|:-:|",
    ]
    for k, g in ev.gate.items():
        op = "≤" if k in ("pbo", "null_pvalue") else "≥"
        L.append(
            f"| {GATE_LABELS.get(k, k)} | {_num(g['value'], 3)} | {op} {_num(g['threshold'], 2)} | "
            f"{'✅' if g['pass'] else '❌'} |"
        )
    L += [
        "",
        "## Données",
        "",
        f"- Source : `{d['source']}`, barres `{d['bar']}`, du {d['start'][:10]} au {d['end'][:10]}.",
        f"- {d['symbols']} contrats ayant figuré dans l'univers point-in-time (≈ {d['avg_universe']:.0f} "
        f"membres en moyenne), {d['rows']:_} échantillons × {d['features']} variables.".replace("_", " "),
        f"- Hors échantillon à partir du {d['oos_start'][:10]}.",
        "",
        "## Qualité de prédiction (IC transversal de Spearman, cible résiduelle nette du funding)",
        "",
        "| Horizon | IC moyen | t (Newey-West) | IC/σ | % périodes > 0 |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, v in ev.ic.items():
        if k.startswith("h") and isinstance(v, dict):
            L.append(
                f"| {k[1:]} barres | {_num(v['ic_mean'], 4)} | {_num(v['ic_t'], 2)} | {_num(v['ic_ir'], 3)} | "
                f"{_pct(v.get('ic_hit', float('nan')))} |"
            )
    models = {k: v for k, v in ev.ic.items() if k.startswith("model_")}
    if models:
        L += [
            "",
            "Par modèle (horizon de détention) : "
            + ", ".join(f"`{k[6:]}` IC {_num(v['ic_mean'], 4)} (t {_num(v['ic_t'], 1)})" for k, v in models.items())
            + ".",
        ]
    if "by_year" in ev.ic:
        L += [
            "",
            "IC réalisé par année : " + ", ".join(f"{y} : {_num(x, 4)}" for y, x in ev.ic["by_year"].items()) + ".",
        ]
    if "market_timing" in ev.ic:
        mt = ev.ic["market_timing"]
        verdict_mt = "franchie" if mt.get("gate") else "non franchie"
        L += [
            "",
            f"Modèle de direction du marché : corrélation {_num(mt['corr'], 4)} (t ≈ {_num(mt['t'], 1)}), "
            f"par année {mt.get('by_year', {})} ; porte propre {verdict_mt}. Sharpe du livre avec exposition "
            f"nette pilotée : {_num(t.get('sharpe_with_market'))} (utilisé en production : "
            f"{'oui' if t.get('market_promoted') else 'non'}).",
        ]
    L += [
        "",
        "## Performance nette du portefeuille (bêta-neutre, maker d'abord)",
        "",
        "| Mesure | Valeur |",
        "|---|---:|",
        f"| Sharpe annualisé (quotidien) | {_num(s.get('sharpe_daily'))} |",
        f"| Intervalle bootstrap 90 % du Sharpe | [{_num(t.get('sharpe_ci_low'))} ; {_num(t.get('sharpe_ci_high'))}] |",
        f"| Rendement annualisé (CAGR) | {_pct(s.get('cagr'))} |",
        f"| Volatilité annualisée | {_pct(s.get('ann_vol'))} |",
        f"| Perte maximale (drawdown) | {_pct(s.get('max_drawdown'))} |",
        f"| Calmar | {_num(s.get('calmar'))} |",
        f"| Pire / meilleur jour | {_pct(s.get('worst_day'), 2)} / {_pct(s.get('best_day'), 2)} |",
        f"| Jours positifs | {_pct(s.get('hit_rate_daily'))} |",
        f"| PnL brut annuel (avant coûts) | {_pct(s.get('gross_pnl_annual'))} |",
        f"| Frais / spread / impact annuels | {_pct(s.get('fees_annual'))} / {_pct(s.get('spread_annual'))} / "
        f"{_pct(s.get('impact_annual'))} |",
        f"| Funding annuel (+ = encaissé) | {_pct(s.get('funding_annual'))} |",
        f"| Rotation annuelle (× capital) | {_num(s.get('turnover_annual'), 0)} |",
        f"| Exposition brute / nette / bêta moyennes | {_num(s.get('avg_gross'))} / {_num(s.get('avg_net'))} / "
        f"{_num(s.get('avg_abs_beta'))} |",
        f"| Positions moyennes | {_num(s.get('avg_positions'), 1)} |",
        f"| Stops catastrophe déclenchés par an | {_num(s.get('stops_annual'), 0)} |",
        "",
        "### Par année",
        "",
        "| Année | Rendement | Sharpe | Drawdown max | P&L jambe acheteuse | P&L jambe vendeuse | Funding | Coûts "
        "| Rotation | Stops |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in ev.yearly.reset_index().iterrows():
        g = row.get
        L.append(
            f"| {int(row['year'])} | {_pct(row['return'])} | {_num(row['sharpe'])} | {_pct(row['max_drawdown'])} | "
            f"{_pct(g('pnl_long', float('nan')))} | {_pct(g('pnl_short', float('nan')))} | "
            f"{_pct(g('funding', float('nan')))} | {_pct(g('costs', float('nan')))} | "
            f"{_num(g('turnover', float('nan')), 0)} | {_num(g('stops', float('nan')), 0)} |"
        )
    L += [
        "",
        "## Tests statistiques",
        "",
        f"- **Probabilistic Sharpe Ratio** (vrai Sharpe > 0) : {_num(t.get('psr'), 3)}.",
        f"- **Deflated Sharpe Ratio** ({int(t.get('n_trials', 1))} essais effectifs comptés) : "
        f"{_num(t.get('dsr'), 3)}.",
        f"- **Nul par permutation** : Sharpe réel au percentile {_pct(t.get('null_percentile'), 0)}, "
        f"p-valeur exacte {_num(t.get('null_pvalue'), 3)} ; "
        f"95ᵉ percentile du nul {_num(t.get('null_sharpe_p95'))} ({len(ev.null_sharpes)} répliques).",
        f"- **Test SPA de Hansen** (p-valeur, H0 : aucun avantage) : {_num(t.get('spa_pvalue'), 3)}.",
        f"- **PBO** sur la grille : {_num(t.get('pbo'), 3)}.",
        f"- **Historique minimal** pour conclure à 95 % : {_num(t.get('min_track_record_days'), 0)} jours.",
        f"- **Stress** : coûts ×2 → Sharpe {_num(t.get('sharpe_costx2'))} ; une barre de latence → "
        f"Sharpe {_num(t.get('sharpe_lag1'))}.",
        "",
    ]
    if ev.halted_at:
        L += [
            f"> **Livre arrêté par le contrôle de drawdown le {str(ev.halted_at)[:10]}** (arrêt dur, ou budget de "
            "risque sous 5 % : le coussin de drawdown est épuisé). Il ne trade quasiment plus ensuite, comme en réel "
            "jusqu'à une reprise humaine ; les statistiques ci-dessus incluent ces jours sans activité.",
            "",
        ]
    nh = ev.nohalt
    if nh:
        years = " ; ".join(f"{y} : {_pct(r)}" for y, r in nh.get("by_year", {}).items())  # type: ignore[union-attr]
        L += [
            "### Économie du signal sans arrêt (diagnostic, hors porte)",
            "",
            "Même stratégie sur toute la période, contrôles de drawdown et de perte journalière désactivés : ce "
            "que le signal rapporte et coûte réellement, année par année.",
            "",
            f"- Sharpe {_num(nh.get('sharpe'))}, CAGR {_pct(nh.get('cagr'))}, drawdown max "
            f"{_pct(nh.get('max_drawdown'))} ;",
            f"- P&L brut {_pct(nh.get('gross_pnl_annual'))}/an contre coûts {_pct(nh.get('costs_annual'))}/an, "
            f"rotation {_num(nh.get('turnover_annual'), 0)}×/an, "
            f"exposition brute moyenne {_num(nh.get('avg_gross'))} ;",
            f"- par année : {years}.",
            "",
        ]
    if len(ev.grid):
        L += [
            "### Grille de construction (base du PBO)",
            "",
            "| Configuration | Sharpe | CAGR | Drawdown | Rotation |",
            "|---|---:|---:|---:|---:|",
        ]
        for cfg_name, row in ev.grid.iterrows():
            L.append(
                f"| `{cfg_name}` | {_num(row['sharpe'])} | {_pct(row['return'])} | {_pct(row['max_dd'])} | "
                f"{_num(row['turnover'], 0)} |"
            )
        L.append("")
    if wf.feature_importance is not None:
        top = wf.feature_importance.head(15)
        L += [
            "## Variables les plus utilisées (gain LightGBM, moyenne des plis)",
            "",
            ", ".join(f"`{k}` {100 * v:.1f} %" for k, v in top.items()),
            "",
        ]
    L += [
        "## Lecture honnête",
        "",
        "Un backtest, même hors échantillon, reste une estimation : l'intervalle de confiance du Sharpe ci-dessus "
        "dit à quel point. La porte de promotion est volontairement sévère ; un résultat qui ne la franchit pas "
        "ne trade pas en réel, quel que soit l'attrait des chiffres. Un résultat qui la franchit démarre en "
        "papier, puis en réel à capital réduit, et reste surveillé par l'IC réalisé en continu.",
        "",
    ]
    return "\n".join(L)


def comparison_table(reports: list[Path]) -> str:
    """Markdown table comparing research reports (``report.json``) side by side, gate verdict included."""
    rows = []
    for path in reports:
        f = path / "report.json" if path.is_dir() else path
        meta = json.loads(f.read_text())
        ev = meta["evaluation"]
        t, s, ic, d = ev["tests"], ev["summary"], ev["ic"], meta["data"]
        H = meta["config"]["portfolio"]["holding_horizon"]
        ich = ic.get(f"h{H}") or next((v for k, v in ic.items() if k.startswith("h")), {})
        fees = sum(float(s.get(k, 0.0) or 0.0) for k in ("fees_annual", "spread_annual", "impact_annual"))
        rows.append(
            [
                f"{meta['config'].get('name', '')} ({f.parent.name})",
                d["bar"],
                f"{d['oos_start'][:10]} → {d['end'][:10]}",
                _num(ich.get("ic_mean"), 4) + f" (t {_num(ich.get('ic_t'), 1)})",
                _pct(float(s.get("gross_pnl_annual", float("nan")))),
                _pct(fees),
                _pct(float(s.get("cagr", float("nan")))),
                _num(float(t.get("sharpe_daily", float("nan")))),
                _pct(float(s.get("max_drawdown", float("nan")))),
                _num(float(t.get("dsr", float("nan"))), 3),
                _num(float(t.get("null_pvalue", float("nan"))), 3),
                _num(float(t.get("pbo", float("nan"))), 2),
                _num(float(t.get("sharpe_costx2", float("nan")))),
                _num(float(t.get("sharpe_lag1", float("nan")))),
                "✅" if ev.get("promoted") else "❌",
            ]
        )
    head = [
        "Config (rapport)",
        "Bougie",
        "Hors échantillon",
        "IC (t)",
        "P&L brut/an",
        "Coûts/an",
        "CAGR net",
        "Sharpe net",
        "Drawdown max",
        "DSR",
        "p nul",
        "PBO",
        "Sharpe coûts×2",
        "Sharpe +1 barre",
        "Promu",
    ]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)

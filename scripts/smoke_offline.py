#!/usr/bin/env python3
"""``make smoke-offline`` : parcours complet HORS LIGNE (§68.2), trois régimes et deux scénarios.

Ce script n'ouvre aucune socket et n'utilise aucun secret. Il enchaîne, pour chaque jeu de données :
fixtures → stockage de test (SQLite mémoire) → rejeu du marché → décision aux frontières de 60 s →
risque (approbation explicite liée au hash) → simulation d'ordre et de fills → comptabilité → rapport
et assertions d'invariants.

Il vérifie aussi ce que le cahier des charges exige de CONSTATER :

- un aller-retour sur un marché plat COÛTE de l'argent (frais, spread) : aucun résultat flatteur ;
- le PnL marqué et le PnL après liquidation simulée sont publiés séparément ;
- deux exécutions du même jeu avec la même graine donnent le même rapport (T70) ;
- « ne rien faire » est un chemin complet et journalisé.

Sortie : ``reports/smoke-offline.json`` et un code de sortie non nul si un invariant échoue.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from okxq.cli_cmds.replay_cmd import _run
from okxq.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml"
DATASETS: tuple[tuple[str, Path], ...] = (
    ("neutre", ROOT / "tests" / "fixtures" / "golden"),
    ("favorable", ROOT / "tests" / "fixtures" / "golden_regimes" / "favorable"),
    ("defavorable", ROOT / "tests" / "fixtures" / "golden_regimes" / "defavorable"),
    ("plat", ROOT / "tests" / "fixtures" / "golden_regimes" / "plat"),
)
SCENARIOS = ("flat", "scripted")


def _strip_ids(report: dict[str, Any]) -> str:
    """Empreinte du rapport hors identifiants aléatoires, pour comparer deux exécutions (T70)."""
    keep = {
        "events_applied": report["events_applied"],
        "boundaries": report["boundaries"],
        "fills": [{k: v for k, v in f.items() if k not in ("execution_key",)} for f in report["fills"]],
        "ledger": report["ledger"],
        "close_out": {k: v for k, v in report["close_out"].items() if k != "ambiguous_paths"},
        "decisions": [d.get("outcome") for d in report["decisions"]],
    }
    return json.dumps(keep, ensure_ascii=False, sort_keys=True, default=str)


async def main() -> int:
    cfg = load_config(CONFIG)
    results: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for regime, dataset in DATASETS:
        if not (dataset / "manifest.json").exists():
            checks.append(
                {
                    "check": f"dataset:{regime}",
                    "ok": False,
                    "detail": f"jeu absent : {dataset} — lancer scripts/build_golden_dataset.py --all-regimes",
                }
            )
            continue
        for scenario in SCENARIOS:
            report = await _run(cfg, dataset, scenario)
            results.append({"regime": regime, "scenario": scenario, **report})
            checks.append(
                {
                    "check": f"invariants:{regime}:{scenario}",
                    "ok": bool(report["ok"]),
                    "detail": "; ".join(
                        f"{i['invariant']}={i['ok']}" for i in report["invariants"] if not i["ok"]
                    )
                    or "tous les invariants vérifiés",
                }
            )
            if scenario == "flat":
                checks.append(
                    {
                        "check": f"no_trade_journalise:{regime}",
                        "ok": all(d.get("outcome") == "NO_TRADE" for d in report["decisions"])
                        and len(report["decisions"]) > 0
                        and len(report["fills"]) == 0,
                        "detail": f"{len(report['decisions'])} décision(s) NO_TRADE, 0 fill",
                    }
                )
            if scenario == "scripted":
                pnl = float(report["strategy_pnl"])
                checks.append(
                    {
                        "check": f"aller_retour_coute:{regime}",
                        # Sur un marché plat, l'aller-retour doit COÛTER : c'est l'exigence §37.
                        "ok": pnl < 0 if regime == "plat" else True,
                        "detail": f"PnL de stratégie {report['strategy_pnl']} (frais {report['ledger']['fees_cum']})",
                    }
                )
                checks.append(
                    {
                        "check": f"pnl_marque_vs_liquide:{regime}",
                        "ok": "marked_equity" in report["close_out"]
                        and "liquidated_equity" in report["close_out"],
                        "detail": (
                            f"marqué {report['close_out'].get('marked_equity')} / "
                            f"liquidé {report['close_out'].get('liquidated_equity')}"
                        ),
                    }
                )

    # Reproductibilité : le même jeu, la même configuration et la même graine donnent le même rapport.
    first = await _run(cfg, DATASETS[0][1], "scripted")
    second = await _run(cfg, DATASETS[0][1], "scripted")
    checks.append(
        {
            "check": "reproductibilite_T70",
            "ok": _strip_ids(first) == _strip_ids(second),
            "detail": "deux exécutions identiques hors identifiants aléatoires",
        }
    )

    ok = all(bool(c["ok"]) for c in checks)
    out = {
        "ok": ok,
        "config": str(CONFIG.relative_to(ROOT)),
        "mode": cfg.mode.value,
        "quality_level": "synthetic",
        "warning": (
            "Jeux de données SYNTHÉTIQUES et scénarios de TEST : ce parcours prouve que la chaîne "
            "fonctionne et que la comptabilité tient, pas qu'une stratégie est rentable."
        ),
        "checks": checks,
        "runs": [
            {
                "regime": r["regime"],
                "scenario": r["scenario"],
                "events_applied": r["events_applied"],
                "boundaries": r["boundaries"],
                "fills": len(r["fills"]),
                "ledger_equity": r["ledger"]["equity"],
                "strategy_pnl": r["strategy_pnl"],
                "fees_cum": r["ledger"]["fees_cum"],
                "funding_cum": r["ledger"]["funding_cum"],
                "marked_equity": r["close_out"].get("marked_equity"),
                "liquidated_equity": r["close_out"].get("liquidated_equity"),
                "residual_exposure": len(r["close_out"].get("residual_exposure") or []),
                "invariants_ok": r["ok"],
            }
            for r in results
        ],
    }
    target = ROOT / "reports" / "smoke-offline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"ok": ok, "checks": len(checks), "runs": len(results), "report": str(target)}, indent=2)
    )
    for check in checks:
        if not check["ok"]:
            print(f"  ÉCHEC {check['check']} : {check['detail']}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

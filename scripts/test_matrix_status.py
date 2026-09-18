#!/usr/bin/env python3
"""Génère docs/test_matrix.md depuis les RÉSULTATS RÉELS de pytest (rapport JUnit).

Usage :
  uv run pytest -q -m "not connected" --junitxml=reports/junit.xml
  uv run python scripts/test_matrix_status.py reports/junit.xml > docs/test_matrix.md

Un test absent du rapport est NOT_RUN ; un test sauté est SKIPPED (avec sa raison) ; jamais PASS par
défaut. Les IDs T01–T70 et leurs libellés viennent du cahier des charges (§64).
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

CASES: dict[str, tuple[str, str]] = {
    "T01": ("Conversion contrats/base/notionnel", "Unités et signe exacts sur fixtures linéaires"),
    "T02": ("Instrument inverse ou devise non supportée", "Rejet explicite avant modèle/ordre"),
    "T03": ("Tick/lot non admissible", "Normalisation contrôlée ou rejet, puis revalidation du risque"),
    "T04": (
        "Métadonnée modifiée en cours de session",
        "Nouvelle version appliquée sans interpréter le passé avec elle",
    ),
    "T05": ("Snapshot suivi d'updates valides", "Book conforme au résultat de référence"),
    "T06": ("Trou de séquence", "Book invalide et aucune nouvelle entrée concernée"),
    "T07": ("Séquences valides mais non consécutives de un", "Pas de faux rejet"),
    "T08": ("Message de maintien sans changement", "Santé et ancienneté correctement distinguées"),
    "T09": ("Reset de séquence", "Comportement conforme au validateur de protocole, sinon resync"),
    "T10": ("Checksum déprécié fixé à zéro", "Aucun calcul CRC incorrect sur le flux concerné"),
    "T11": ("Quantité de niveau égale à zéro", "Niveau supprimé et top-of-book reconstruit"),
    "T12": ("Carnet croisé, quantité négative ou NaN", "Données invalides exclues des décisions"),
    "T13": ("REST et WS non raccordables", "Fusion interdite"),
    "T14": ("Donnée future ajoutée à l'historique", "Décisions antérieures inchangées"),
    "T15": ("Événement ancien reçu tard", "Indisponible avant sa réception réelle"),
    "T16": ("Feature intrabougie vs bougie clôturée", "Pas de confusion ni de fuite de clôture"),
    "T17": ("Normalisation ou sélection sur test", "Détectée par les assertions de provenance"),
    "T18": ("Actif délisté/renommé", "Univers point-in-time et position encore comptabilisée"),
    "T19": ("Labels chevauchants", "Purge et disponibilités temporelles correctes"),
    "T20": ("Stacking OOF", "Aucun entraînement sur la fenêtre prédite"),
    "T21": ("Label censuré", "Exclusion/masque explicite, pas rendement nul inventé"),
    "T22": (
        "Changement du test final après consultation",
        "Statut indépendant perdu et revalidation requise",
    ),
    "T23": ("Frais maker/taker et rebate", "Signes, devises et montants exacts"),
    "T24": ("Prix exécutables + spread déduit à nouveau", "Erreur de double comptage bloquée"),
    "T25": ("Funding traversé ou non", "Flux uniquement aux règlements concernés"),
    "T26": ("Funding final utilisé comme feature antérieure", "Fuite détectée"),
    "T27": ("Ordre maker non exécuté", "Pas de position/PnL fictif"),
    "T28": ("Toucher d'un prix sans volume suffisant", "Pas de fill maker garanti"),
    "T29": ("Liquidité insuffisante pour un IOC", "Fill partiel et reliquat non inventé"),
    "T30": ("Deux ordres consomment la même profondeur", "Pas de double allocation du volume simulé"),
    "T31": ("Fill pendant une annulation", "Position et frais mis à jour une seule fois"),
    "T32": ("ACK perdu après envoi", "UNKNOWN, réservation maintenue, aucun retry aveugle"),
    "T33": ("Redémarrage avec UNKNOWN", "Réconciliation avant nouvelle entrée"),
    "T34": ("Message/fill dupliqué", "Ledger et position idempotents"),
    "T35": ("Fill reçu avant ACK", "État cohérent sans perte de l'exécution"),
    "T36": ("Réutilisation d'un client ID terminal", "Interdite par notre journal"),
    "T37": ("Réponse partiellement réussie", "Traitement item par item"),
    "T38": ("Rejet de reduce-only", "Pas de retry sans cette protection"),
    "T39": ("Passage long -> short", "Clôture et nouvelle ouverture distinguées, risques revérifiés"),
    "T40": ("Rounding casse la neutralité/marge", "Candidat corrigé sous contraintes ou rejeté"),
    "T41": ("Solveur timeout/infeasible/NaN", "Aucune stratégie de secours non validée"),
    "T42": ("Une seule jambe d'un basket est exécutée", "Risque transitoire plafonné et réconciliation"),
    "T43": ("Ordres opposés/UNKNOWN en attente", "Exposition pessimiste prise en compte"),
    "T44": ("Risque journalier après redémarrage", "Pertes et halt conservés"),
    "T45": ("Dépôt/retrait externe", "Performance et high-water mark non artificiellement améliorés"),
    "T46": ("Deux gateways concurrents", "Un seul chemin de signature/envoi effectif"),
    "T47": ("Perte du bail/base", "Ancien writer incapable d'augmenter le risque"),
    "T48": ("Intention/approbation expirée", "Ordre non envoyé"),
    "T49": ("Modification d'un payload approuvé", "Hash invalide, nouvelle approbation nécessaire"),
    "T50": ("Panne JEV", "Risque/protection actifs, fallback explicitement validé ou halt"),
    "T51": ("Réponse JEV tardive", "Exclue du snapshot passé et non antidatée"),
    "T52": ("Probabilités JEV invalides", "Rejet typé, sans ordre déclenché"),
    "T53": ("Version JEV inattendue", "Nouvelle validation requise"),
    "T54": ("Ticker ambigu ou source contradictoire", "Mapping non inventé, qualité explicite"),
    "T55": ("Injection dans un document", "Aucune permission, aucun secret ni action arbitraire accessibles"),
    "T56": ("URL vers réseau privé ou redirection malveillante", "SSRF bloquée"),
    "T57": ("Document rejoué depuis cache", "Âge et provenance initiaux conservés"),
    "T58": ("Source ou publication manquante", "Absence explicitée, jamais timestamp inventé"),
    "T59": (
        "Stop prévu mais non confirmé",
        "Position marquée non protégée et alerte/réduction selon politique",
    ),
    "T60": ("Cancel All After déclenché", "Ordres concernés traités, position non supposée fermée"),
    "T61": (
        "Processus stratégie mort, heartbeat indépendant vivant",
        "Pas de maintien aveugle de prises de risque orphelines",
    ),
    "T62": ("Exchange indisponible pendant flatten", "État pending et exposition résiduelle visible"),
    "T63": ("DEMO en échec", "Aucun basculement réseau/clés vers LIVE"),
    "T64": ("LIVE sans approbations complètes", "Refus avant connexion privée de trading"),
    "T65": ("Action UI non autorisée/CSRF", "Aucun effet et événement d'audit"),
    "T66": ("Secret dans logs/artefacts/image", "Test de sécurité échoué et livraison bloquée"),
    "T67": ("Restauration de sauvegarde", "Données restaurées et réconciliation avant reprise"),
    "T68": ("Saturation disque/queue/CPU", "Backpressure/arrêt contrôlé, pas de perte silencieuse critique"),
    "T69": ("Tests hors ligne sans réseau", "Parcours complet sur fixtures reproductible"),
    "T70": ("Même dataset/config/seed", "Résultat identique dans la tolérance documentée"),
}

_ID_RE = re.compile(r"test_(T\d{2})")


def main(junit_path: str) -> int:
    root = ET.parse(junit_path).getroot()
    by_id: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for tc in root.iter("testcase"):
        totals["tests"] += 1
        name = tc.get("name", "")
        classname = tc.get("classname", "")
        status, detail = "PASS", ""
        if tc.find("failure") is not None:
            status, detail = "FAIL", (tc.find("failure").get("message") or "")[:120]
            totals["failures"] += 1
        elif tc.find("error") is not None:
            status, detail = "ERROR", (tc.find("error").get("message") or "")[:120]
            totals["errors"] += 1
        elif tc.find("skipped") is not None:
            status, detail = "SKIPPED", (tc.find("skipped").get("message") or "")[:120]
            totals["skipped"] += 1
        for m in _ID_RE.finditer(name):
            by_id[m.group(1)].append((f"{classname}::{name}", status, detail))
    lines = [
        "# Matrice de tests (§64) — générée depuis les résultats réels",
        "",
        f"Source : `{Path(junit_path).name}` — {totals['tests']} tests exécutés, {totals['failures']} échecs, "
        f"{totals['errors']} erreurs, {totals['skipped']} sautés. Un ID sans test exécuté est `NOT_RUN`.",
        "",
        "| ID | Cas à tester | Résultat attendu | Niveau | Tests | Statut |",
        "|---|---|---|---|---|---|",
    ]
    summary: dict[str, int] = defaultdict(int)
    for tid, (case, expected) in CASES.items():
        tests = by_id.get(tid, [])
        if not tests:
            status = "NOT_RUN"
            names = "—"
            level = "—"
        else:
            statuses = {s for _, s, _ in tests}
            status = (
                "FAIL"
                if ("FAIL" in statuses or "ERROR" in statuses)
                else ("SKIPPED" if statuses == {"SKIPPED"} else "PASS")
            )
            names = "<br>".join(
                f"`{n.split('::')[0].split('.')[-1]}::{n.split('::')[-1]}`" for n, _, _ in tests[:4]
            )
            if len(tests) > 4:
                names += f"<br>… (+{len(tests) - 4})"
            levels = {n.split(".")[1] if "." in n else "?" for n, _, _ in tests}
            level = "/".join(sorted(levels))
        summary[status] += 1
        lines.append(f"| {tid} | {case} | {expected} | {level} | {names} | **{status}** |")
    lines += ["", "## Synthèse", ""]
    for k in ("PASS", "FAIL", "SKIPPED", "NOT_RUN"):
        lines.append(f"- {k} : {summary.get(k, 0)}")
    lines += [
        "",
        "Les tests de propriété (invariants d'exposition, conservation comptable, déduplication, arrondis,",
        "monotonie des quantités exécutées, impossibilité d'un ordre sans approbation) sont dans `tests/property/`.",
        "Les tests connectés (`-m connected`) ne sont jamais lancés par la CI ; sans clé ils restent NOT_RUN.",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage : test_matrix_status.py <junit.xml>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))

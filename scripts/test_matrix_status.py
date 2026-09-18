#!/usr/bin/env python3
"""Génère `docs/test_matrix.md` depuis un rapport JUnit RÉELLEMENT produit par pytest (§64).

Pourquoi ce script existe : une matrice de tests écrite à la main finit toujours par mentir. Elle est
rédigée au moment où le test est écrit, puis le test est renommé, désactivé, ou jamais exécuté — et le
tableau, lui, continue d'afficher « PASS ». Ici le tableau ne peut dire que ce que le rapport JUnit
contient : chaque ligne est adossée à des cas de test nommés, avec leur résultat observé.

Usage :
    python scripts/test_matrix_status.py --junit reports/junit.xml --out docs/test_matrix.md

Règles de classement (elles sont le cœur de l'honnêteté du document) :

* `PASS`    — au moins un cas de test porte l'identifiant et aucun n'a échoué ;
* `FAIL`    — au moins un cas a échoué (`failure`) ou est tombé en erreur (`error`) ;
* `NOT_RUN` — aucun cas ne porte l'identifiant, OU tous les cas qui le portent ont été sautés.

Un test sauté n'est PAS un test qui passe : il n'a rien vérifié. Un identifiant sans test est un trou
déclaré, jamais masqué. La phrase qui décrit la sélection exécutée est DÉDUITE du rapport
(`selection_sentence`) et non écrite en dur : elle devient fausse dès qu'on fournit un accès
manquant, ce qui est exactement le cas que ce document ne doit pas rater.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import Element

# Libellés repris MOT POUR MOT de la matrice §64 du cahier des charges : la colonne « cas à tester » et
# la colonne « résultat attendu » sont l'exigence, pas un résumé du test qui l'implémente.
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
    "T55": (
        "Injection dans un document",
        "Aucune permission, aucun secret ni action arbitraire accessibles",
    ),
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
    "T68": (
        "Saturation disque/queue/CPU",
        "Backpressure/arrêt contrôlé, pas de perte silencieuse critique",
    ),
    "T69": ("Tests hors ligne sans réseau", "Parcours complet sur fixtures reproductible"),
    "T70": ("Même dataset/config/seed", "Résultat identique dans la tolérance documentée"),
}

# Les tests du dépôt nomment leur identifiant : `test_T66_...`, `test_T27_T28_...`. Le caractère qui
# précède ne doit pas être alphanumérique (sinon l'horodatage `2026-09-18T08:00:00Z` d'un identifiant
# paramétré serait lu comme « T08 ») et le suivant ne doit pas être un chiffre (pour ne pas confondre
# « T7 » d'un hypothétique « T700 »).
ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])T(\d{2})(?!\d)")

# Niveau de test déduit du module : `tests/unit/...` → `unit`. Le niveau est une information de §64
# (« exigence -> test -> niveau -> résultat ») et il n'est écrit nulle part ailleurs.
LEVEL_PATTERN = re.compile(r"^tests[./]([a-z0-9_]+)")

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_RUN = "NOT_RUN"

# Ordre d'affichage de la synthèse : les états qui bloquent une livraison d'abord.
SUMMARY_ORDER = (STATUS_FAIL, STATUS_NOT_RUN, STATUS_PASS)


@dataclass(frozen=True, slots=True)
class TestCaseResult:
    """Un cas de test tel que le rapport JUnit le décrit — jamais tel qu'on l'espérait."""

    classname: str
    name: str
    outcome: str  # "passed" | "failed" | "error" | "skipped"
    detail: str

    @property
    def level(self) -> str:
        match = LEVEL_PATTERN.match(self.classname)
        return match.group(1) if match else "?"

    @property
    def short_name(self) -> str:
        """`tests.unit.test_orderbook::test_T06_...` → `test_orderbook::test_T06_...`.

        Le module suffit pour retrouver le test ; le chemin complet rendrait la colonne illisible.
        """
        module = self.classname.rsplit(".", 1)[-1] if self.classname else "?"
        return f"{module}::{self.name}"

    @property
    def function_name(self) -> str:
        """Nom sans l'identifiant de paramétrage : `test_T56_x[169.254.169.254]` → `test_T56_x`."""
        return self.name.split("[", 1)[0]


@dataclass(frozen=True, slots=True)
class Totals:
    """Compteurs recalculés depuis les cas eux-mêmes, pas lus dans les attributs du rapport."""

    collected: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0


def _first_text(case: Element, tag: str) -> str:
    """Message d'un `failure`/`error`/`skipped`, tronqué : la matrice résume, le rapport détaille."""
    node = case.find(tag)
    if node is None:
        return ""
    message = node.get("message") or (node.text or "")
    flattened = " ".join(message.split())
    return flattened[:160]


def parse_junit(path: Path) -> tuple[list[TestCaseResult], Totals]:
    """Lit le rapport JUnit et rend les cas observés.

    Le fichier est produit localement par notre propre pytest : c'est un artefact de confiance, d'où
    la bibliothèque standard. Les documents EXTERNES, eux, ne sont jamais parsés ainsi — ils passent
    par `defusedxml` dans `okxq.jev.source_connectors`.
    """
    root = ElementTree.parse(path).getroot()
    cases: list[TestCaseResult] = []
    collected = passed = failed = errored = skipped = 0
    for element in root.iter("testcase"):
        collected += 1
        if element.find("failure") is not None:
            outcome, detail = "failed", _first_text(element, "failure")
            failed += 1
        elif element.find("error") is not None:
            outcome, detail = "error", _first_text(element, "error")
            errored += 1
        elif element.find("skipped") is not None:
            outcome, detail = "skipped", _first_text(element, "skipped")
            skipped += 1
        else:
            outcome, detail = "passed", ""
            passed += 1
        cases.append(
            TestCaseResult(
                classname=element.get("classname", ""),
                name=element.get("name", ""),
                outcome=outcome,
                detail=detail,
            )
        )
    totals = Totals(collected=collected, passed=passed, failed=failed, errored=errored, skipped=skipped)
    return cases, totals


def index_by_requirement(cases: list[TestCaseResult]) -> dict[str, list[TestCaseResult]]:
    """Associe chaque identifiant T01–T70 aux cas qui le nomment.

    Un même test peut couvrir deux exigences (`test_T27_T28_...`) : il apparaît alors sur les deux
    lignes. Un identifiant hors T01–T70 est ignoré : il ne correspond à aucune exigence du §64.
    """
    index: dict[str, list[TestCaseResult]] = defaultdict(list)
    for case in cases:
        for match in ID_PATTERN.finditer(f"{case.name} {case.classname}"):
            requirement = f"T{match.group(1)}"
            if requirement in CASES:
                index[requirement].append(case)
    return index


def classify(cases: list[TestCaseResult]) -> tuple[str, str]:
    """Rend (statut, preuve). La preuve dit POURQUOI, surtout quand le statut n'est pas PASS."""
    if not cases:
        # Aucun test ne porte cet identifiant : le trou est déclaré, il n'est pas comblé par un PASS.
        return STATUS_NOT_RUN, "aucun test ne porte cet identifiant"
    broken = [c for c in cases if c.outcome in ("failed", "error")]
    if broken:
        return STATUS_FAIL, broken[0].detail or "échec sans message"
    ran = [c for c in cases if c.outcome == "passed"]
    if not ran:
        # Tous sautés : le code n'a pas été exercé, donc rien n'est vérifié.
        motifs = sorted({c.detail for c in cases if c.detail}) or ["motif non fourni"]
        return STATUS_NOT_RUN, f"sauté ({len(cases)}) : {motifs[0]}"
    note = f"{len(ran)} cas vert{'s' if len(ran) > 1 else ''}"
    left_out = len(cases) - len(ran)
    if left_out:
        note += f", {left_out} sauté(s) — la couverture est partielle"
    return STATUS_PASS, note


def format_cases(cases: list[TestCaseResult], *, limit: int = 3) -> str:
    """Colonne « Tests » : les fonctions, dédupliquées, sans le bruit du paramétrage."""
    if not cases:
        return "—"
    seen: list[str] = []
    for case in cases:
        module = case.classname.rsplit(".", 1)[-1] if case.classname else "?"
        label = f"{module}::{case.function_name}"
        if label not in seen:
            seen.append(label)
    shown = [f"`{label}`" for label in seen[:limit]]
    if len(seen) > limit:
        shown.append(f"… (+{len(seen) - limit})")
    return "<br>".join(shown)


def format_levels(cases: list[TestCaseResult]) -> str:
    return "/".join(sorted({case.level for case in cases})) if cases else "—"


def selection_sentence(cases: list[TestCaseResult]) -> str:
    """Décrit la sélection RÉELLEMENT exécutée, en la lisant du rapport.

    Cette phrase affirmait en dur que les tests `integration` et `connected` n'étaient pas dans le
    rapport. C'était vrai le jour où elle a été écrite. Le jour où une base PostgreSQL est fournie,
    ces tests s'exécutent et la phrase devient fausse — dans le document dont l'honnêteté est
    précisément la raison d'être. Elle est donc DÉDUITE des cas présents.
    """
    niveaux = sorted({case.level for case in cases})
    phrases = [f"Niveaux présents dans ce rapport : {', '.join(f'`{n}`' for n in niveaux)}."]
    if "integration" in niveaux:
        phrases.append(
            "Les tests `integration` (schéma et migrations sur PostgreSQL) ont été RÉELLEMENT "
            "exécutés contre un serveur PostgreSQL 16."
        )
    else:
        phrases.append(
            "Les tests `integration` (PostgreSQL) ne sont pas dans ce rapport : les exigences qui "
            "en dépendent restent `NOT_RUN` faute de base, jamais `PASS`."
        )
    phrases.append(
        "La suite ne contient AUCUN test `connected` : rien ici n'a été confronté au vrai OKX ni au "
        "vrai service TypeSafe. Toute exigence qui demande un appel réel reste `NOT_RUN` par "
        "construction, et un `PASS` sur fixtures ne la remplace pas."
    )
    return " ".join(phrases)


def build_document(junit_path: Path, cases: list[TestCaseResult], totals: Totals) -> str:
    index = index_by_requirement(cases)
    statuses: dict[str, str] = {}
    rows: list[str] = []
    for requirement, (case_label, expected) in CASES.items():
        matched = index.get(requirement, [])
        status, evidence = classify(matched)
        statuses[requirement] = status
        rows.append(
            f"| {requirement} | {case_label} | {expected} | {format_levels(matched)} "
            f"| {format_cases(matched)} | **{status}** | {evidence} |"
        )
    counts = {state: sum(1 for s in statuses.values() if s == state) for state in SUMMARY_ORDER}
    not_run_ids = [rid for rid, state in statuses.items() if state == STATUS_NOT_RUN]
    failed_ids = [rid for rid, state in statuses.items() if state == STATUS_FAIL]

    lines: list[str] = [
        "# Matrice de tests T01–T70 (§64)",
        "",
        "<!-- Fichier GÉNÉRÉ par `scripts/test_matrix_status.py`. Ne pas éditer à la main : toute",
        "     correction manuelle serait écrasée, et surtout elle ne serait adossée à aucune preuve. -->",
        "",
        f"Source : `{junit_path.as_posix()}` — {totals.collected} cas collectés, "
        f"{totals.passed} verts, {totals.failed} échecs, {totals.errored} erreurs, "
        f"{totals.skipped} sautés.",
        "",
        selection_sentence(cases),
        "",
        "Lecture des statuts :",
        "",
        f"- `{STATUS_PASS}` — au moins un cas nommant l'identifiant a été exécuté et aucun n'a échoué ;",
        f"- `{STATUS_FAIL}` — au moins un cas a échoué ou est tombé en erreur ;",
        f"- `{STATUS_NOT_RUN}` — aucun cas ne porte l'identifiant, ou tous ont été sautés. Un test",
        "  sauté n'a rien vérifié : il ne devient pas vert parce que la suite est verte.",
        "",
        "Un `PASS` signifie « ce comportement est vérifié sur fixtures hors ligne ». Il ne signifie "
        "ni « vérifié contre OKX », ni « rentable » : aucune ligne de ce tableau n'est une mesure de "
        "marché.",
        "",
        "| ID | Cas à tester (§64) | Résultat attendu (§64) | Niveau | Tests | Statut | Preuve |",
        "|---|---|---|---|---|---|---|",
        *rows,
        "",
        "## Synthèse",
        "",
        f"| Statut | Nombre sur {len(CASES)} |",
        "|---|---|",
        *[f"| `{state}` | {counts[state]} |" for state in SUMMARY_ORDER],
        "",
    ]
    if failed_ids:
        lines += [
            f"**Exigences en échec ({len(failed_ids)})** : {', '.join(failed_ids)}. "
            "Un échec bloque la capacité correspondante.",
            "",
        ]
    else:
        lines += ["Aucune exigence en échec dans cette exécution.", ""]

    # Un tableau sans FAIL alors que la suite a des échecs donnerait une impression fausse : les cas
    # rouges qui ne nomment aucun identifiant §64 n'apparaissent nulle part ailleurs. On les nomme.
    unmapped_failures = [
        case
        for case in cases
        if case.outcome in ("failed", "error") and not any(case in matched for matched in index.values())
    ]
    if unmapped_failures:
        listed = ", ".join(f"`{c.short_name}`" for c in unmapped_failures[:8])
        if len(unmapped_failures) > 8:
            listed += f" … (+{len(unmapped_failures) - 8})"
        lines += [
            f"**Attention — {len(unmapped_failures)} cas en échec hors matrice** : {listed}. "
            "Ces cas ne nomment aucun identifiant §64, donc aucune ligne ci-dessus ne passe à "
            "`FAIL` ; la suite est pourtant rouge. Le tableau ne doit pas se lire comme un état de "
            "santé global de la suite.",
            "",
        ]
    lines += [
        f"**Exigences non exécutées ({len(not_run_ids)})** : "
        f"{', '.join(not_run_ids) if not_run_ids else 'aucune'}.",
        "",
        "Chacune reste bloquante pour la capacité qu'elle devait valider (§71.1) : rien ici n'est "
        "présenté comme couvert par autre chose.",
        "",
        "## Ce que ce tableau ne dit pas",
        "",
        "- Les tests de propriété livrés (`tests/property/`) portent sur la conservation d'une "
        "position, la direction et la grille des arrondis, l'arithmétique monétaire, les invariants "
        "de carnet, l'idempotence du remplacement d'un niveau et la monotonie des séquences. Ils ne "
        "nomment aucun identifiant §64 : ils n'apparaissent donc sur aucune ligne, et les propriétés "
        "exigées par §64 qui manquent encore (invariants d'exposition, déduplication, monotonie des "
        "quantités exécutées, impossibilité d'un ordre sans approbation) ne sont pas couvertes ici.",
        "- Aucun test connecté (clé OKX DEMO/LIVE, clé TypeSafe) n'a été exécuté : ni le connecteur "
        "privé, ni un appel JEV réel ne sont vérifiés ici.",
        "- Les chiffres produits par les jeux de données synthétiques ou golden servent à exercer les "
        "pipelines. Ils ne constituent aucune preuve d'avantage de marché.",
        "",
        "- Les tests frontend (`node --test frontend/tests/*.test.js`, cible `make ui-test`) ne "
        "passent pas par pytest : ils ne sont pas dans ce rapport et ne comptent sur aucune ligne.",
        "",
        "## Régénération",
        "",
        "`reports/` n'est pas versionné : le rapport source doit être reproduit avant de régénérer ce",
        "fichier, sinon le tableau décrirait une exécution que personne ne peut retrouver.",
        "",
        "```sh",
        "# Les tests `integration` exigent une base PostgreSQL désignée ; sans elle ils se sautent, et",
        "# les exigences correspondantes retombent à NOT_RUN — ce que le document dira alors.",
        "export OKXQ_TEST_DATABASE_URL=postgresql+psycopg://okxq@127.0.0.1:5432/okxq_test",
        "pytest -q -p no:randomly --junit-xml=reports/junit.xml -p no:cacheprovider",
        "python scripts/test_matrix_status.py --junit reports/junit.xml --out docs/test_matrix.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Génère docs/test_matrix.md depuis un rapport JUnit de pytest (§64).",
    )
    parser.add_argument(
        "--junit",
        type=Path,
        default=Path("reports/junit.xml"),
        help="rapport JUnit produit par pytest (--junit-xml)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/test_matrix.md"),
        help="fichier Markdown à écrire",
    )
    args = parser.parse_args(argv)
    junit_path: Path = args.junit
    out_path: Path = args.out

    if not junit_path.is_file():
        # Pas de rapport = pas de matrice. Écrire un tableau « tout NOT_RUN » serait une affirmation
        # sans source ; mieux vaut échouer et laisser l'opérateur lancer les tests.
        print(f"rapport JUnit introuvable : {junit_path}", file=sys.stderr)
        return 2
    try:
        cases, totals = parse_junit(junit_path)
    except ElementTree.ParseError as exc:
        print(f"rapport JUnit illisible ({junit_path}) : {exc}", file=sys.stderr)
        return 2
    if not cases:
        print(f"aucun cas de test dans {junit_path}", file=sys.stderr)
        return 2

    document = build_document(junit_path, cases, totals)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")

    index = index_by_requirement(cases)
    covered = sum(1 for rid in CASES if index.get(rid))
    print(
        f"{out_path} écrit — {totals.collected} cas lus, "
        f"{covered}/{len(CASES)} identifiants adossés à au moins un test."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

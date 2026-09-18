"""Rend les documents de `docs/api_contracts/` À PARTIR du code et du manifeste.

POURQUOI générer plutôt qu'écrire. Un contrat d'API recopié à la main dérive : l'allowlist du
gateway gagne un endpoint, le manifeste change une limite de débit, et le document continue de
décrire l'état d'avant — en gardant l'autorité d'un document. Ici la source de vérité est le code
(`ALLOWLIST`, `FORBIDDEN_PATH_PREFIXES`, les constantes du client JEV) et le manifeste de
capacités ; le document n'en est qu'un rendu, et `tests/contract/test_api_contracts_docs.py`
échoue si l'un des deux bouge sans l'autre.

Usage : `uv run python scripts/render_api_contracts.py [--check]`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from okxq.exchange.okx.rest_private import ALLOWLIST, FORBIDDEN_PATH_PREFIXES  # noqa: E402
from okxq.jev.client import (  # noqa: E402
    API_KEY_ENV,
    DEFAULT_MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUS,
    USER_AGENT,
)

MANIFEST = ROOT / "infra" / "capability_manifest.json"
OUT_DIR = ROOT / "docs" / "api_contracts"
GENERATED = (
    "<!-- Document GÉNÉRÉ par scripts/render_api_contracts.py — ne pas modifier à la main.\n"
    "     La source de vérité est le code et infra/capability_manifest.json. -->\n"
)


def _rate(limit: dict[str, object] | None) -> str:
    if not limit:
        return "non documentée"
    return f"{limit['requests']}/{limit['per_seconds']} s ({limit['scope']})"


def render_okx_public() -> str:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rest = {k: v for k, v in manifest["operations"].items() if v["kind"] == "rest"}
    ws = {k: v for k, v in manifest["operations"].items() if v["kind"] == "ws"}
    lines = [
        GENERATED,
        "# OKX API v5 — contrat public (§46, §49)",
        "",
        f"Référence normative : <{manifest['reference']}>. "
        f"**Date de vérification : {manifest['verified_at']}.**",
        "",
        "Statut de validation : **vérifié sur fixtures, non validé en connexion** dans cette session.",
        "Aucun appel réseau n'a été émis vers OKX ; chaque opération ci-dessous est couverte par une",
        "fixture enregistrée, et les tests marqués `contract` rejouent ces fixtures.",
        "",
        f"{manifest['notes']}",
        "",
        "## Domaines par profil de compte",
        "",
        "| Profil | REST | WebSocket public | WebSocket business |",
        "|---|---|---|---|",
    ]
    for name, profile in manifest["region_profiles"].items():
        lines.append(
            f"| `{name}` | `{profile['rest_base_url']}` | `{profile['ws_public_url']}` "
            f"| `{profile.get('ws_business_url', '—')}` |"
        )
    lines += [
        "",
        "Le profil vient de `OKX_ACCOUNT_REGION_PROFILE`. Il n'est jamais choisi pour contourner une",
        "restriction géographique ou contractuelle.",
        "",
        "## Opérations REST",
        "",
        "| Opération | Méthode | Chemin | Environnements | Permission | Limite de débit | Pagination"
        " | Idempotent | Erreurs | Fixture |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, op in rest.items():
        lines.append(
            f"| `{name}` | {op['method']} | `{op['path']}` | {', '.join(op['environment'])} "
            f"| {op['permission']} | {_rate(op.get('rate_limit'))} "
            f"| {op.get('pagination') or 'aucune'} | {'oui' if op['idempotent'] else 'non'} "
            f"| {', '.join(op['errors']) or '—'} | `{op.get('fixture', '—')}` |"
        )
    lines += [
        "",
        "## Canaux WebSocket",
        "",
        "| Canal | Environnements | Limite de débit | Fixture |",
        "|---|---|---|---|",
    ]
    for op in ws.values():
        channel = op.get("channel") or "—"
        lines.append(
            f"| `{channel}` | {', '.join(op['environment'])} | {_rate(op.get('rate_limit'))} "
            f"| `{op.get('fixture', '—')}` |"
        )
    lines += [
        "",
        "## Profondeur exigée par famille de features",
        "",
        "| Feature | Profondeur minimale | Description |",
        "|---|---|---|",
    ]
    for name, feature in manifest["features"].items():
        lines.append(f"| `{name}` | {feature['min_depth']} | {feature['description']} |")
    lines += [
        "",
        "## Continuité du carnet",
        "",
        "La continuité fait foi sur `seqId` / `prevSeqId`. Depuis le changelog OKX du 23 juin 2026, le",
        "checksum des canaux `books`, `books-l2-tbt` et `books50-l2-tbt` est **déprécié** et vaut 0 : un",
        "code qui le vérifierait rejetterait tous les messages. La fixture",
        "`tests/fixtures/okx/ws_books_checksum_zero.json` fixe ce comportement, et",
        "`ws_books_gap.json` / `ws_books_reset.json` couvrent la rupture de séquence et la",
        "resynchronisation.",
        "",
        "## Ce qui n'est pas couvert",
        "",
        "- Aucune mesure de latence réelle, aucun comportement sous limitation de débit observé : les",
        "  limites du manifeste sont traitées comme des bornes supérieures par le token bucket interne.",
        "- Les valeurs de `availability` (régions, types de compte) viennent de la documentation et ne",
        "  sont pas vérifiées pour le compte effectivement utilisé.",
        "",
    ]
    return "\n".join(lines)


def render_okx_private() -> str:
    reads = [e for e in ALLOWLIST.values() if e.safe_read]
    writes = [e for e in ALLOWLIST.values() if not e.safe_read]
    lines = [
        GENERATED,
        "# OKX API v5 — contrat privé (§46, §57, §58, §60)",
        "",
        "Référence normative : <https://app.okx.com/docs-v5/en/>. **Date de vérification : 2026-09-18.**",
        "",
        "Statut de validation : **vérifié sur fixtures et vecteurs de signature construits ;**",
        "**DEMO non exécuté, LIVE jamais exécuté.** Aucun appel authentifié n'a été émis. Les tests",
        "correspondants (T50, T51, T63) sont donc NOT_RUN faute d'identifiants, et non « réussis ».",
        "",
        "## Signature",
        "",
        "```",
        "prehash = timestamp + method + requestPath + body",
        "sign    = Base64( HMAC-SHA256( secretKey, prehash ) )",
        "```",
        "",
        "- `timestamp` REST : ISO 8601 UTC en millisecondes (`2020-12-08T09:08:57.715Z`) ;",
        "- `requestPath` inclut la chaîne de requête (`/api/v5/account/balance?ccy=USDT`) ;",
        "- `body` est la chaîne JSON **exactement telle qu'envoyée** : on signe les octets émis, jamais",
        "  une re-sérialisation, sinon la signature ne correspond plus au corps reçu ;",
        "- en-têtes : `OK-ACCESS-KEY`, `OK-ACCESS-SIGN`, `OK-ACCESS-TIMESTAMP`, `OK-ACCESS-PASSPHRASE` ;",
        "- `x-simulated-trading: 1` en DEMO ;",
        "- login WebSocket : `timestamp` en secondes epoch, signature sur",
        '  `timestamp + "GET" + "/users/self/verify"`.',
        "",
        "Les vecteurs des tests sont **construits par cette formule** ; ils ne proviennent pas d'OKX et",
        "ne prouvent donc pas que le serveur l'accepte. Seul un appel DEMO le prouverait.",
        "",
        "## Où vivent les identifiants",
        "",
        "Les clés ne sont lues que dans le rôle `gateway`, à l'endroit unique qui construit l'adaptateur.",
        "Le collecteur, la recherche, l'interface et le worker JEV ne les reçoivent jamais, et chaque rôle",
        "refuse de démarrer si elles sont présentes dans son environnement",
        "(`assert_credentials_separation`). Un secret présent dans un processus est lisible par tout ce",
        "qui y tourne : la vérification porte donc sur l'absence, pas sur la discipline d'usage.",
        "",
        "## Allowlist — lectures",
        "",
        "| Opération | Méthode | Chemin | Famille | Budget | Portée |",
        "|---|---|---|---|---|---|",
    ]
    for e in reads:
        lines.append(
            f"| `{e.name}` | {e.method} | `{e.path}` | {e.family} | {e.budget.value} | {e.scope_kind} |"
        )
    lines += [
        "",
        "## Allowlist — écritures",
        "",
        "| Opération | Méthode | Chemin | Famille | Budget | Portée |",
        "|---|---|---|---|---|---|",
    ]
    for e in writes:
        lines.append(
            f"| `{e.name}` | {e.method} | `{e.path}` | {e.family} | {e.budget.value} | {e.scope_kind} |"
        )
    lines += [
        "",
        "Tout endpoint hors de ces deux tables est refusé **avant tout réseau**",
        "(`EndpointNotAllowedError`). Une allowlist refuse par défaut ; une liste noire laisse passer",
        "tout ce qu'on a oublié d'y écrire.",
        "",
        "## Préfixes refusés par construction",
        "",
        "Refusés quel que soit l'appelant, même si une allowlist future les contenait :",
        "",
    ]
    for prefix in FORBIDDEN_PATH_PREFIXES:
        lines.append(f"- `{prefix}`")
    lines += [
        "",
        "Aucun mouvement de fonds (retrait, transfert, conversion, emprunt), aucun changement de mode de",
        "compte ou de levier, aucune fermeture de position en masse. Ces opérations ne sont pas",
        "nécessaires à la stratégie, et leur présence dans un processus qui détient les clés suffirait à",
        "transformer un défaut de logique en perte de fonds.",
        "",
        "## Idempotence et réconciliation",
        "",
        "- Chaque ordre porte un `clientOrderId` déterministe ; `(account_scope, client_order_id)` est",
        "  unique **en base**, pas seulement dans le code (vérifié sur PostgreSQL).",
        "- Un `place_order` dont la réponse est perdue n'est jamais rejoué à l'aveugle : l'état est relu",
        "  (`order`, `orders_pending`, `fills`) avant toute nouvelle tentative.",
        "- `cancel_all_after` est armé comme filet de sécurité côté exchange ; son échec est un",
        "  déclencheur de protection, pas un avertissement.",
        "",
    ]
    return "\n".join(lines)


def render_typesafe() -> str:
    fixtures = ROOT / "tests" / "fixtures" / "jev"
    cases = json.loads((fixtures / "invalid_responses.json").read_text(encoding="utf-8"))
    questions = json.loads((ROOT / "configs" / "jev_questions.v1.json").read_text(encoding="utf-8"))
    lines = [
        GENERATED,
        "# TypeSafe — contrat JEV (§49, §50, §52)",
        "",
        "Endpoint : `POST https://api.typesafe.ai/v1/systemone`. Référence consultée :",
        "<https://docs.typesafe.ai/api>. **Date de vérification : 2026-09-18.**",
        "",
        "Statut de validation : **vérifié sur fixtures — aucun appel réel.** Le contrat ci-dessous décrit",
        "ce que cette implémentation exige et rejette ; il ne prouve pas que le service répond ainsi.",
        "Les tests portent la marque `contract`, jamais `connected`.",
        "",
        "## Ce qui part, et ce qui ne part jamais",
        "",
        'La requête est un `JevRequest` strict (`extra="forbid"`) : modèle, jeu de questions, nom',
        "canonique et symbole de l'actif, titre et texte du document, documents antérieurs éventuels.",
        "",
        "N'y figurent **jamais** : aucun identifiant OKX, aucune position, aucun montant, aucune equity,",
        "aucune identité de détenteur, aucune stratégie. JEV reçoit sa propre clé et le minimum de texte",
        "nécessaire à la question posée. C'est une séparation de conception, pas une consigne d'usage :",
        "le schéma refuse tout champ supplémentaire.",
        "",
        f"La clé est lue dans `{API_KEY_ENV}` au seul moment de poser l'en-tête `Authorization`. Elle",
        "n'apparaît ni dans les journaux, ni dans les erreurs, ni dans un `repr`.",
        "",
        f"En-tête `User-Agent` : `{USER_AGENT}`.",
        "",
        "## Jeu de questions",
        "",
        f"Version : `{questions['model']}`, {len(questions['questions'])} questions.",
        "",
        "| Question | Type |",
        "|---|---|",
    ]
    for name, spec in questions["questions"].items():
        lines.append(f"| `{name}` | {spec['type']} |")
    lines += [
        "",
        "Trois types de réponses : `choice` (catégories avec distribution de probabilités), `noul`",
        "(probabilité seule) et `score` (niveau entier avec distribution). Le jeu de questions est",
        "haché (`question_set_hash`) et ce hachage entre dans la clé de cache : changer une question",
        "invalide les évaluations antérieures au lieu de les réutiliser silencieusement.",
        "",
        "## Réponse attendue",
        "",
        "Exemple de référence : `tests/fixtures/jev/response_reference.json`. Sont exigés :",
        "",
        "- `model` : doit **égaler** le modèle demandé ; une réponse d'un autre modèle est refusée ;",
        "- une réponse pour **chaque** question demandée, du type attendu ;",
        "- les distributions somment à 1 (tolérance 1e-3) et chaque probabilité est dans [0, 1] ;",
        "- `usage` : `inputTokens`, `outputTokens`, `totalTokens` — l'absence d'usage est refusée, car",
        "  sans lui le budget de dépense ne peut pas être tenu ;",
        "- `provider_metadata.typesafe.confidence` : optionnel, repris tel quel.",
        "",
        "Le décodage est strict : `NaN`, `Infinity` et `-Infinity` littéraux sont refusés",
        "(`tests/fixtures/jev/response_invalid_nan.txt`). Un parser permissif transformerait une",
        "réponse absurde en feature silencieuse.",
        "",
        "## Réponses invalides refusées",
        "",
        f"{len(cases)} cas enregistrés dans `tests/fixtures/jev/invalid_responses.json`, chacun rejoué :",
        "",
    ]
    for case in cases:
        lines.append(f"- `{case['name']}`")
    lines += [
        "",
        "## Erreurs et rejeu",
        "",
        "| Situation | Rejouable |",
        "|---|---|",
        f"| {', '.join(str(code) for code in sorted(RETRYABLE_STATUS))} | oui |",
        "| timeouts et erreurs de transport | oui |",
        "| 401 (clé refusée) | non |",
        "| 400 `max_tokens_exceeded` (état trop gros) | non |",
        "| 422 (schéma refusé par le fournisseur) | non |",
        "| réponse invalide (`JevContractError`) | non |",
        "",
        f"`Retry-After` est respecté mais **borné à {DEFAULT_MAX_RETRY_AFTER_SECONDS:.0f} s** : un",
        "fournisseur qui demanderait d'attendre une heure ne doit pas immobiliser la boucle de décision.",
        "Une deadline murale totale (`timeout_total_ms`) est mesurée par l'horloge injectée, distincte",
        "des timeouts de socket ; aucune tentative ne démarre au-delà.",
        "",
        "## Dégradation",
        "",
        "Une panne JEV **n'est jamais un déclencheur de protection** (§50). Elle relève de la politique",
        "d'entrées : sans évaluation fraîche, les features JEV sont absentes et l'entrée qui en dépend",
        "n'est pas prise. L'interface affiche « Non disponible », jamais zéro — une évaluation absente et",
        "une évaluation neutre ne sont pas la même information.",
        "",
        "## Ce qui n'est pas couvert",
        "",
        "- Aucun appel réel : ni latence observée, ni comportement sous limitation, ni forme exacte des",
        "  corps d'erreur du fournisseur.",
        "- Le coût facturé par appel n'est pas vérifié ; le budget quotidien s'appuie sur `usage` tel que",
        "  le fournisseur le déclare.",
        "",
    ]
    return "\n".join(lines)


RENDERERS = {
    "okx_public.md": render_okx_public,
    "okx_private.md": render_okx_private,
    "typesafe_jev.md": render_typesafe,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="échoue si un document a dérivé")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, render in RENDERERS.items():
        target = OUT_DIR / name
        content = render()
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            target.write_text(content, encoding="utf-8")
            print(f"écrit {target.relative_to(ROOT)}")
    if stale:
        print("documents périmés : " + ", ".join(stale), file=sys.stderr)
        print("relancer : uv run python scripts/render_api_contracts.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

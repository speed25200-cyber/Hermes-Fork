# Contrats d'API — index (§46, §49, §58)

Chaque contrat fournisseur est documenté avec : méthode, chemin, environnement, permission, limites,
schéma, pagination, idempotence, erreurs, référence officielle et **date de vérification**. Les
fixtures représentatives sont dans `tests/fixtures/` et servent aux tests marqués `contract`.

| Contrat | Document | Manifeste / fixtures | Statut de validation |
|---|---|---|---|
| OKX API v5 — public (REST + WebSocket : instruments, carnet, trades, bougies, mark/index, funding, OI) | `docs/api_contracts/okx_public.md` | `infra/capability_manifest.json`, `tests/fixtures/okx/` | vérifié sur fixtures ; **non validé en connexion** dans cette session |
| OKX API v5 — privé (compte, ordres, fills, protections, Cancel All After, authentification HMAC, DEMO) | `docs/api_contracts/okx_private.md` | `tests/fixtures/okx/`, vecteurs de signature | vérifié sur fixtures ; **DEMO non exécuté** |
| TypeSafe — JEV (`POST /v1/systemone`, Choice/Noul/Score, confidence, usage) | `docs/api_contracts/typesafe_jev.md` | `tests/fixtures/jev/` | vérifié sur fixtures ; **aucun appel réel** |
| API interne opérateur (FastAPI, `/api/v1/*`, `/health/*`, canaux de compatibilité de l'interface) | `docs/openapi.json` (généré par `okxq api export-openapi`), `docs/ui.md` | tests `tests/unit/test_api_*.py` | testé hors ligne |

Règles transverses :

- Les domaines REST/WS OKX viennent du profil de compte (`OKX_ACCOUNT_REGION_PROFILE`), jamais choisis
  pour contourner une restriction géographique ou contractuelle.
- Le gateway n'expose qu'une allowlist d'opérations : transferts, retraits, conversions et changements
  de compte/levier sont refusés au niveau du code.
- Une divergence entre un exemple et la référence normative suit la référence effective et crée un test ;
  aucun parser permissif ne masque une divergence.
- Correction datée : depuis le changelog OKX du 23 juin 2026, le checksum des canaux `books`,
  `books-l2-tbt` et `books50-l2-tbt` est déprécié (valeur 0) ; la continuité `seqId/prevSeqId` fait foi.

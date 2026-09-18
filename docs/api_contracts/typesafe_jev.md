<!-- Document GÉNÉRÉ par scripts/render_api_contracts.py — ne pas modifier à la main.
     La source de vérité est le code et src/okxq/exchange/okx/capability_manifest.json. -->

# TypeSafe — contrat JEV (§49, §50, §52)

Endpoint : `POST https://api.typesafe.ai/v1/systemone`. Référence consultée :
<https://docs.typesafe.ai/api>. **Date de vérification : 2026-09-18.**

Statut de validation : **vérifié sur fixtures — aucun appel réel.** Le contrat ci-dessous décrit
ce que cette implémentation exige et rejette ; il ne prouve pas que le service répond ainsi.
Les tests portent la marque `contract`, jamais `connected`.

## Ce qui part, et ce qui ne part jamais

La requête est un `JevRequest` strict (`extra="forbid"`) : modèle, jeu de questions, nom
canonique et symbole de l'actif, titre et texte du document, documents antérieurs éventuels.

N'y figurent **jamais** : aucun identifiant OKX, aucune position, aucun montant, aucune equity,
aucune identité de détenteur, aucune stratégie. JEV reçoit sa propre clé et le minimum de texte
nécessaire à la question posée. C'est une séparation de conception, pas une consigne d'usage :
le schéma refuse tout champ supplémentaire.

La clé est lue dans `TYPESAFE_API_KEY` au seul moment de poser l'en-tête `Authorization`. Elle
n'apparaît ni dans les journaux, ni dans les erreurs, ni dans un `repr`.

En-tête `User-Agent` : `okx-quant-jev/0.1 (jev-client)`.

## Jeu de questions

Version : `jev-1.13.0`, 6 questions.

| Question | Type |
|---|---|
| `event_kind` | choice |
| `asset_relevance` | noul |
| `reported_severity` | score |
| `novelty_vs_prior` | score |
| `reported_operational_impact` | score |
| `source_confirms` | noul |

Trois types de réponses : `choice` (catégories avec distribution de probabilités), `noul`
(probabilité seule) et `score` (niveau entier avec distribution). Le jeu de questions est
haché (`question_set_hash`) et ce hachage entre dans la clé de cache : changer une question
invalide les évaluations antérieures au lieu de les réutiliser silencieusement.

## Réponse attendue

Exemple de référence : `tests/fixtures/jev/response_reference.json`. Sont exigés :

- `model` : doit **égaler** le modèle demandé ; une réponse d'un autre modèle est refusée ;
- une réponse pour **chaque** question demandée, du type attendu ;
- les distributions somment à 1 (tolérance 1e-3) et chaque probabilité est dans [0, 1] ;
- `usage` : `inputTokens`, `outputTokens`, `totalTokens` — l'absence d'usage est refusée, car
  sans lui le budget de dépense ne peut pas être tenu ;
- `provider_metadata.typesafe.confidence` : optionnel, repris tel quel.

Le décodage est strict : `NaN`, `Infinity` et `-Infinity` littéraux sont refusés
(`tests/fixtures/jev/response_invalid_nan.txt`). Un parser permissif transformerait une
réponse absurde en feature silencieuse.

## Réponses invalides refusées

18 cas enregistrés dans `tests/fixtures/jev/invalid_responses.json`, chacun rejoué :

- `T52_probability_above_one`
- `T52_probability_negative`
- `T52_boolean_for_number`
- `T52_string_for_number`
- `T52_probability_missing`
- `T53_choice_sum_inconsistent`
- `T53_unknown_category`
- `T53_missing_category`
- `T53_unexpected_model_version`
- `T53_model_missing`
- `T53_score_without_distribution`
- `T53_score_out_of_levels`
- `T53_score_as_float`
- `T53_distribution_wrong_length`
- `T53_distribution_sum_inconsistent`
- `T53_choice_outside_categories`
- `T53_missing_answer`
- `T53_missing_usage`

## Erreurs et rejeu

| Situation | Rejouable |
|---|---|
| 408, 425, 429, 500, 502, 503, 504, 529 | oui |
| timeouts et erreurs de transport | oui |
| 401 (clé refusée) | non |
| 400 `max_tokens_exceeded` (état trop gros) | non |
| 422 (schéma refusé par le fournisseur) | non |
| réponse invalide (`JevContractError`) | non |

`Retry-After` est respecté mais **borné à 5 s** : un
fournisseur qui demanderait d'attendre une heure ne doit pas immobiliser la boucle de décision.
Une deadline murale totale (`timeout_total_ms`) est mesurée par l'horloge injectée, distincte
des timeouts de socket ; aucune tentative ne démarre au-delà.

## Dégradation

Une panne JEV **n'est jamais un déclencheur de protection** (§50). Elle relève de la politique
d'entrées : sans évaluation fraîche, les features JEV sont absentes et l'entrée qui en dépend
n'est pas prise. L'interface affiche « Non disponible », jamais zéro — une évaluation absente et
une évaluation neutre ne sont pas la même information.

## Ce qui n'est pas couvert

- Aucun appel réel : ni latence observée, ni comportement sous limitation, ni forme exacte des
  corps d'erreur du fournisseur.
- Le coût facturé par appel n'est pas vérifié ; le budget quotidien s'appuie sur `usage` tel que
  le fournisseur le déclare.

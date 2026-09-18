<!-- Document GÉNÉRÉ par scripts/render_api_contracts.py — ne pas modifier à la main.
     La source de vérité est le code et src/okxq/exchange/okx/capability_manifest.json. -->

# OKX API v5 — contrat public (§46, §49)

Référence normative : <https://app.okx.com/docs-v5/en/>. **Date de vérification : 2026-09-18.**

Statut de validation : **vérifié sur fixtures, non validé en connexion** dans cette session.
Aucun appel réseau n'a été émis vers OKX ; chaque opération ci-dessous est couverte par une
fixture enregistrée, et les tests marqués `contract` rejouent ces fixtures.

Manifeste de capacités OKX v5 (public uniquement). Les limites de débit sont les valeurs documentées à la date de vérification et sont traitées comme des bornes supérieures par le token bucket interne. Les domaines viennent du profil de compte (OKX_ACCOUNT_REGION_PROFILE) et ne sont jamais choisis pour contourner une restriction.

## Domaines par profil de compte

| Profil | REST | WebSocket public | WebSocket business |
|---|---|---|---|
| `default` | `https://www.okx.com` | `wss://ws.okx.com:8443/ws/v5/public` | `wss://ws.okx.com:8443/ws/v5/business` |
| `demo` | `https://www.okx.com` | `wss://wspap.okx.com:8443/ws/v5/public` | `wss://wspap.okx.com:8443/ws/v5/business` |

Le profil vient de `OKX_ACCOUNT_REGION_PROFILE`. Il n'est jamais choisi pour contourner une
restriction géographique ou contractuelle.

## Opérations REST

| Opération | Méthode | Chemin | Environnements | Permission | Limite de débit | Pagination | Idempotent | Erreurs | Fixture |
|---|---|---|---|---|---|---|---|---|---|
| `rest.public.time` | GET | `/api/v5/public/time` | production, demo | none | 10/2 s (ip) | aucune | oui | 50011, 50013 | `tests/fixtures/okx/rest_time.json` |
| `rest.public.instruments` | GET | `/api/v5/public/instruments` | production, demo | none | 20/2 s (ip+instType) | aucune | oui | 50011, 51000, 51001 | `tests/fixtures/okx/rest_instruments.json` |
| `rest.public.open-interest` | GET | `/api/v5/public/open-interest` | production, demo | none | 20/2 s (ip+instType) | aucune | oui | 50011, 51000 | `tests/fixtures/okx/rest_open_interest.json` |
| `rest.public.funding-rate-history` | GET | `/api/v5/public/funding-rate-history` | production, demo | none | 10/2 s (ip+instId) | {'style': 'cursor_ts', 'cursor_fields': ['after', 'before'], 'cursor_semantics': 'after = enregistrements STRICTEMENT antérieurs à ts ; before = strictement postérieurs', 'ts_field': 'fundingTime', 'order': 'newest_first', 'max_limit': 100} | oui | 50011, 51000, 51001 | `tests/fixtures/okx/rest_funding_rate_history.json` |
| `rest.public.mark-price` | GET | `/api/v5/public/mark-price` | production, demo | none | 10/2 s (ip+instId) | aucune | oui | 50011, 51000 | `tests/fixtures/okx/rest_mark_price.json` |
| `rest.public.price-limit` | GET | `/api/v5/public/price-limit` | production, demo | none | 20/2 s (ip) | aucune | oui | 50011, 51000 | `tests/fixtures/okx/rest_price_limit.json` |
| `rest.market.books` | GET | `/api/v5/market/books` | production, demo | none | 40/2 s (ip) | aucune | oui | 50011, 51000, 51001 | `tests/fixtures/okx/rest_books.json` |
| `rest.market.history-candles` | GET | `/api/v5/market/history-candles` | production, demo | none | 20/2 s (ip) | {'style': 'cursor_ts', 'cursor_fields': ['after', 'before'], 'cursor_semantics': 'after = bougies STRICTEMENT antérieures à ts ; before = strictement postérieures', 'ts_field': '0', 'order': 'newest_first', 'max_limit': 100} | oui | 50011, 51000, 51001 | `tests/fixtures/okx/rest_history_candles.json` |
| `rest.market.history-trades` | GET | `/api/v5/market/history-trades` | production, demo | none | 10/2 s (ip) | {'style': 'cursor_ts_or_tradeId', 'cursor_fields': ['after', 'before', 'type'], 'cursor_semantics': 'type=2 : after/before sont des horodatages ; type=1 : des tradeId', 'ts_field': 'ts', 'order': 'newest_first', 'max_limit': 100} | oui | 50011, 51000, 51001 | `tests/fixtures/okx/rest_history_trades.json` |
| `rest.market.tickers` | GET | `/api/v5/market/tickers` | production, demo | none | 20/2 s (ip) | aucune | oui | 50011, 51000 | `tests/fixtures/okx/rest_tickers.json` |

## Canaux WebSocket

| Canal | Environnements | Limite de débit | Fixture |
|---|---|---|---|
| `books` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_books_snapshot.json` |
| `books5` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_books5.json` |
| `bbo-tbt` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_bbo_tbt.json` |
| `books50-l2-tbt` | production | 3/1 s (connection) | `None` |
| `books-l2-tbt` | production | 3/1 s (connection) | `None` |
| `trades` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_trades.json` |
| `candle1m` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_candle1m.json` |
| `mark-price` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_mark_price.json` |
| `index-tickers` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_index_tickers.json` |
| `funding-rate` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_funding_rate.json` |
| `open-interest` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_open_interest.json` |
| `instruments` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/ws_instruments.json` |
| `tickers` | production, demo | 3/1 s (connection) | `tests/fixtures/okx/rest_tickers.json` |
| `—` | production, demo | 3/1 s (ip) | `tests/fixtures/okx/ws_events.json` |

## Profondeur exigée par famille de features

| Feature | Profondeur minimale | Description |
|---|---|---|
| `top_of_book` | 1 | meilleur bid/ask, mid, spread |
| `depth_within_bps` | 5 | profondeur cumulée dans une bande en points de base |
| `imbalance_l10` | 10 | déséquilibre sur 10 niveaux |
| `l2_full_depth` | 400 | profondeur complète (400 niveaux) |

## Continuité du carnet

La continuité fait foi sur `seqId` / `prevSeqId`. Depuis le changelog OKX du 23 juin 2026, le
checksum des canaux `books`, `books-l2-tbt` et `books50-l2-tbt` est **déprécié** et vaut 0 : un
code qui le vérifierait rejetterait tous les messages. La fixture
`tests/fixtures/okx/ws_books_checksum_zero.json` fixe ce comportement, et
`ws_books_gap.json` / `ws_books_reset.json` couvrent la rupture de séquence et la
resynchronisation.

## Ce qui n'est pas couvert

- Aucune mesure de latence réelle, aucun comportement sous limitation de débit observé : les
  limites du manifeste sont traitées comme des bornes supérieures par le token bucket interne.
- Les valeurs de `availability` (régions, types de compte) viennent de la documentation et ne
  sont pas vérifiées pour le compte effectivement utilisé.

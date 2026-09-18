<!-- Document GÉNÉRÉ par scripts/render_api_contracts.py — ne pas modifier à la main.
     La source de vérité est le code et src/okxq/exchange/okx/capability_manifest.json. -->

# OKX API v5 — contrat privé (§46, §57, §58, §60)

Référence normative : <https://app.okx.com/docs-v5/en/>. **Date de vérification : 2026-09-18.**

Statut de validation : **vérifié sur fixtures et vecteurs de signature construits ;**
**DEMO non exécuté, LIVE jamais exécuté.** Aucun appel authentifié n'a été émis. Les tests
correspondants (T50, T51, T63) sont donc NOT_RUN faute d'identifiants, et non « réussis ».

## Signature

```
prehash = timestamp + method + requestPath + body
sign    = Base64( HMAC-SHA256( secretKey, prehash ) )
```

- `timestamp` REST : ISO 8601 UTC en millisecondes (`2020-12-08T09:08:57.715Z`) ;
- `requestPath` inclut la chaîne de requête (`/api/v5/account/balance?ccy=USDT`) ;
- `body` est la chaîne JSON **exactement telle qu'envoyée** : on signe les octets émis, jamais
  une re-sérialisation, sinon la signature ne correspond plus au corps reçu ;
- en-têtes : `OK-ACCESS-KEY`, `OK-ACCESS-SIGN`, `OK-ACCESS-TIMESTAMP`, `OK-ACCESS-PASSPHRASE` ;
- `x-simulated-trading: 1` en DEMO ;
- login WebSocket : `timestamp` en secondes epoch, signature sur
  `timestamp + "GET" + "/users/self/verify"`.

Les vecteurs des tests sont **construits par cette formule** ; ils ne proviennent pas d'OKX et
ne prouvent donc pas que le serveur l'accepte. Seul un appel DEMO le prouverait.

## Où vivent les identifiants

Les clés ne sont lues que dans le rôle `gateway`, à l'endroit unique qui construit l'adaptateur.
Le collecteur, la recherche, l'interface et le worker JEV ne les reçoivent jamais, et chaque rôle
refuse de démarrer si elles sont présentes dans son environnement
(`assert_credentials_separation`). Un secret présent dans un processus est lisible par tout ce
qui y tourne : la vérification porte donc sur l'absence, pas sur la discipline d'usage.

## Allowlist — lectures

| Opération | Méthode | Chemin | Famille | Budget | Portée |
|---|---|---|---|---|---|
| `account_config` | GET | `/api/v5/account/config` | account.config | general | account |
| `balance` | GET | `/api/v5/account/balance` | account.read | reconciliation | account |
| `positions` | GET | `/api/v5/account/positions` | account.read | reconciliation | account |
| `trade_fee` | GET | `/api/v5/account/trade-fee` | account.read | general | account |
| `leverage_info` | GET | `/api/v5/account/leverage-info` | account.read | general | account |
| `bills` | GET | `/api/v5/account/bills` | account.bills | reconciliation | account |
| `orders_pending` | GET | `/api/v5/trade/orders-pending` | trade.read | reconciliation | account |
| `order` | GET | `/api/v5/trade/order` | trade.read | reconciliation | account |
| `fills` | GET | `/api/v5/trade/fills` | trade.read | reconciliation | account |
| `fills_history` | GET | `/api/v5/trade/fills-history` | trade.fills_history | reconciliation | account |
| `algo_pending` | GET | `/api/v5/trade/orders-algo-pending` | trade.read | reconciliation | account |
| `instruments` | GET | `/api/v5/public/instruments` | public.download | download | ip |

## Allowlist — écritures

| Opération | Méthode | Chemin | Famille | Budget | Portée |
|---|---|---|---|---|---|
| `place_order` | POST | `/api/v5/trade/order` | trade.order | trade | instrument |
| `batch_orders` | POST | `/api/v5/trade/batch-orders` | trade.batch | trade | instrument |
| `cancel_order` | POST | `/api/v5/trade/cancel-order` | trade.order | cancel | instrument |
| `amend_order` | POST | `/api/v5/trade/amend-order` | trade.order | trade | instrument |
| `cancel_all_after` | POST | `/api/v5/trade/cancel-all-after` | trade.cancel_all_after | heartbeat | account |
| `place_algo` | POST | `/api/v5/trade/order-algo` | trade.algo | protection | instrument |
| `cancel_algos` | POST | `/api/v5/trade/cancel-algos` | trade.algo | protection | instrument |

Tout endpoint hors de ces deux tables est refusé **avant tout réseau**
(`EndpointNotAllowedError`). Une allowlist refuse par défaut ; une liste noire laisse passer
tout ce qu'on a oublié d'y écrire.

## Préfixes refusés par construction

Refusés quel que soit l'appelant, même si une allowlist future les contenait :

- `/api/v5/asset/`
- `/api/v5/finance/`
- `/api/v5/users/`
- `/api/v5/account/set-position-mode`
- `/api/v5/account/set-leverage`
- `/api/v5/account/set-account-level`
- `/api/v5/account/set-isolated-mode`
- `/api/v5/account/set-auto-loan`
- `/api/v5/account/set-greeks`
- `/api/v5/account/borrow-repay`
- `/api/v5/account/quick-margin-borrow-repay`
- `/api/v5/account/spot-manual-borrow-repay`
- `/api/v5/account/set-riskOffset-type`
- `/api/v5/account/activate-option`
- `/api/v5/account/set-fee-type`
- `/api/v5/copytrading/`
- `/api/v5/rfq/`
- `/api/v5/sprd/`
- `/api/v5/tradingBot/`
- `/api/v5/trade/mass-cancel`
- `/api/v5/trade/close-position`

Aucun mouvement de fonds (retrait, transfert, conversion, emprunt), aucun changement de mode de
compte ou de levier, aucune fermeture de position en masse. Ces opérations ne sont pas
nécessaires à la stratégie, et leur présence dans un processus qui détient les clés suffirait à
transformer un défaut de logique en perte de fonds.

## Idempotence et réconciliation

- Chaque ordre porte un `clientOrderId` déterministe ; `(account_scope, client_order_id)` est
  unique **en base**, pas seulement dans le code (vérifié sur PostgreSQL).
- Un `place_order` dont la réponse est perdue n'est jamais rejoué à l'aveugle : l'état est relu
  (`order`, `orders_pending`, `fills`) avant toute nouvelle tentative.
- `cancel_all_after` est armé comme filet de sécurité côté exchange ; son échec est un
  déclencheur de protection, pas un avertissement.

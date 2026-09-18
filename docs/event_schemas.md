# Schémas des événements de marché (contrat partagé)

Tous les événements circulent sous la forme `okxq.domain.events.EventEnvelope`. Le champ `payload`
suit les schémas ci-dessous. **Tous les nombres sont des chaînes** (Decimal-safe) ; les horodatages
économiques sont en millisecondes epoch (`*_ms`) ; `receive_ts`/`available_at` sont portés par
l'enveloppe. `ingest_seq` est strictement croissant par source et sert de départage stable des
événements à horodatage égal (ordre de réception enregistré, §34).

Les jeux de données golden (`tests/fixtures/golden/`) sont des fichiers `events.jsonl` (une enveloppe
JSON par ligne, `model_dump(mode="json")`) accompagnés d'un `manifest.json` (`dataset`, `schema_version`,
`instruments`, `first_available_at`, `last_available_at`, `rows`, `sha256`, `quality_level`, `notes`).

| event_type | payload |
|---|---|
| `instrument` | `{inst_id, inst_type:"SWAP", ct_val, ct_val_ccy, ct_type:"linear", ct_mult:"1", settle_ccy:"USDT", tick_sz, lot_sz, min_sz, state, lever, list_time_ms}` |
| `book.snapshot` | `{inst_id, channel, seq_id, prev_seq_id, checksum, ts_ms, bids:[[price, qty_contracts], ...], asks:[[price, qty_contracts], ...]}` — niveaux triés (bids décroissants, asks croissants) |
| `book.update` | même forme ; `qty_contracts:"0"` supprime le niveau ; une quantité est un REMPLACEMENT, pas un delta |
| `trade` | `{inst_id, trade_id, price, qty_contracts, side:"buy"\|"sell", ts_ms}` — `side` est le côté **taker** (convention OKX vérifiée par fixture) |
| `candle.1m` | `{inst_id, ts_ms (ouverture), open, high, low, close, vol_contracts, vol_base, vol_quote, confirm:"0"\|"1"}` — seules les bougies `confirm:"1"` sont clôturées |
| `mark_price` | `{inst_id, mark_px, ts_ms}` |
| `index_price` | `{inst_id, idx_px, ts_ms}` |
| `funding` | `{inst_id, funding_rate, next_funding_rate, funding_time_ms, next_funding_time_ms, settled:bool, realized_rate}` — `settled:false` = estimation disponible avant règlement ; `settled:true` = montant réglé (`realized_rate`) |
| `open_interest` | `{inst_id, oi_contracts, oi_base, ts_ms}` |

Règles :

- `exchange_ts` de l'enveloppe = `ts_ms` du payload converti ; `receive_ts` = heure de réception locale ;
  `available_at` ≥ `receive_ts` (après validation/normalisation).
- Un événement reçu tard reste indisponible avant son `receive_ts` réel (T15). Une correction crée un
  nouvel événement (`schema_version` inchangé, `payload_hash` différent), jamais une réécriture.
- Le replay ordonne par `(available_at, ingest_seq)`.

# API MEMEBOT (DEMO ONLY)

Specifica completa OpenAPI 3: [`openapi.json`](openapi.json) (esportata da FastAPI). Documentazione interattiva
sul backend in esecuzione: `/docs` (Swagger) e `/redoc`.

* Tutte le risposte hanno l'header `X-Memebot-Mode: DEMO-ONLY` e i JSON di stato contengono `"demo_only": true`.
* **Autenticazione facoltativa**: se il backend ha `API_TOKEN`, invia `X-API-Key: <token>` (o `?token=` sul WebSocket).
  Sempre aperti: `/`, `/api/health`, `/docs`, `/openapi.json`, `/redoc`.
* Qualsiasi corpo JSON contenente campi come `private_key`, `secret_key`, `seed_phrase`, `mnemonic`, `keypair`,
  `signed_transaction`… viene rifiutato con **HTTP 400** (non esistono campi del genere).
* Date in ISO 8601 UTC. Importi in **SOL virtuali**.

## Stato
| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/api/health` | Stato backend, DB, scanner, motore, prezzo SOL/USD |
| GET | `/api/status` | Heartbeat di ogni servizio e statistiche per fonte (richieste, errori, 429, latenza) |
| GET | `/api/dashboard` | Saldo, equity, P&L giornaliero/totale, posizioni aperte, token monitorati, `tokens_new_hour`, ultimi eventi, `last_market_data_at` |

## Token / scanner
| GET | `/api/tokens?sort=score\|new\|volume\|liquidity\|mcap\|change&risk=LOW,MEDIUM&q=&limit=100` | Token monitorati con punteggio 0-100 e rischio |
|---|---|---|
| GET | `/api/tokens/new?minutes=60&source=pumpfun&limit=100` | Nuovi lanci |
| GET | `/api/tokens/{mint}` | Dettaglio: metriche, holder, authority, componenti del punteggio, motivazioni, disclaimer |
| GET | `/api/tokens/{mint}/chart?hours=6` | Serie storica prezzo/liquidità/volume (snapshot reali raccolti dallo scanner) |

## Paper trading (simulato)
| GET | `/api/portfolio` | Portafoglio virtuale |
|---|---|---|
| POST | `/api/account/reset` | `{"initial_capital_sol": 2.0}` – azzera conto, posizioni e storico simulati |
| POST | `/api/trade/buy` | `{"mint","amount_sol","stop_loss_pct?","take_profit_pct?","trailing_stop_pct?"}` – acquisto SIMULATO |
| POST | `/api/trade/sell` | `{"position_id","fraction":1.0}` – vendita SIMULATA |
| GET | `/api/positions?status=open\|closed\|all&origin=manual\|sniper\|copy` | Posizioni |
| GET | `/api/orders?status=filled\|failed` | Ordini simulati con slippage, commissioni, latenza, motivo di mancata esecuzione |
| GET | `/api/history` | Operazioni chiuse |
| GET | `/api/analytics` | Curva equity, win rate, profit factor, P&L per strategia, distribuzioni |

## Impostazioni
| GET | `/api/settings` | Tutte le impostazioni (engine, sniper, copy, capitale iniziale) |
|---|---|---|
| PUT | `/api/settings/sniper` | Filtri e parametri sniper DEMO |
| PUT | `/api/settings/engine` | Parametri simulazione (fee, slippage, latenza, failure rate, età massima prezzo) |
| PUT | `/api/settings/copy` | Regole copy trading DEMO |

## Copy trading DEMO (solo indirizzi pubblici)
| GET/POST | `/api/copy/wallets` | Elenco / aggiunta `{"address","label?","amount_sol":0.1,"copy_sells":true}` |
|---|---|---|
| PATCH/DELETE | `/api/copy/wallets/{address}` | Modifica / rimozione |
| GET | `/api/copy/trades?wallet=` | Swap rilevati on-chain e stato della copia simulata |
| GET | `/api/copy/leaderboard` | Classifica per P&L simulato |

## Eventi e notifiche
| GET | `/api/events?after_id=0&types=take_profit,stop_loss` | Eventi (usato anche dall'aggiornamento in background dell'app) |
|---|---|---|
| POST | `/api/devices` | Registrazione token APNs (solo se configurato APNs lato server) |

Tipi evento: `new_token`, `token_passed_filters`, `trade_opened`, `trade_closed`, `take_profit`, `stop_loss`,
`trailing_stop`, `copy_detected`, `order_failed`, `scanner_issue`, `account_reset`.

## WebSocket `/ws`
Messaggi server → app:
```json
{"type":"hello","demo_only":true,"last_event_id":123}
{"type":"dashboard","data":{ ...come /api/dashboard... }}   // ogni 5 s
{"type":"event","data":{"id":124,"type":"take_profit","title":"...","body":"...","data":{...}}}
{"type":"pong"}
```
App → server: `"ping"` (testo) ogni 20 s. Parametri: `?after_id=<id>` per riprendere gli eventi, `?token=` se `API_TOKEN` è attivo.

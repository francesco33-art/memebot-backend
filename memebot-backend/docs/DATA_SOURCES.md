# Fonti dati MEMEBOT – verifica reale (13/11/2026)

Tutti gli endpoint sotto sono stati **chiamati davvero** dal backend/box il 13/11/2026 (CET).
Le risposte di esempio sono in [`docs/samples/`](samples/). Nessuna fonte richiede chiavi a pagamento.
Ogni host passa dal limitatore di frequenza `app/http.py` (token bucket per host, backoff, pausa su HTTP 429)
e dal **DEMO guard** (`app/demo_guard.py`), che blocca qualsiasi endpoint di swap/trading/invio transazioni.

| Fonte | Endpoint usati | Esito | Limite documentato | Limite usato da MEMEBOT | Uso |
|---|---|---|---|---|---|
| **PumpPortal** (dati Pump.fun, non ufficiale) | `wss://pumpportal.fun/api/data` → `subscribeNewToken`, `subscribeMigration` | ✅ WebSocket aperto, eventi `create` ricevuti in tempo reale (`samples/pumpportal_newtoken.json`) | stream dati gratuiti senza chiave; i flussi per singolo token/account e l'API di trading sono a pagamento/chiave | 1 connessione, riconnessione con backoff | Nuovi lanci Pump.fun in tempo reale, migrazioni |
| **Pump.fun** (API frontend) | `frontend-api.pump.fun/coins/latest`, `frontend-api-v3.pump.fun/coins/latest` | ❌ **HTTP 403** (Cloudflare) | non pubblica / non documentata | non usata | Sostituita da PumpPortal + Dexscreener |
| **Dexscreener** | `/token-profiles/latest/v1`, `/token-boosts/latest/v1` | ✅ 200 | 60 richieste/min | ≤ 240/min complessive per host | Scoperta token con profilo/boost |
| | `/tokens/v1/solana/{fino a 30 mint}`, `/token-pairs/v1/solana/{mint}` | ✅ 200 (`samples/dexscreener_tokens_v1.json`) | 300 richieste/min | idem | Prezzo, liquidità, volume, transazioni buy/sell 5m/1h/24h, FDV, età coppia |
| | `/latest/dex/search` | ✅ 200 | 300/min | idem | Ricerca |
| **GeckoTerminal** | `/api/v2/networks/solana/new_pools`, `/pools/multi/{indirizzi}` | ✅ 200 (`samples/geckoterminal_new_pools.json`) | ~10 chiamate/min (piano gratuito; una FAQ storica cita 30/min) | 8/min | Nuove pool, compratori/venditori unici |
| **Jupiter Price API v3** | `https://lite-api.jup.ag/price/v3?ids=...` | ✅ 200 (`samples/jupiter_price_v3.json`) | lite-api gratuita senza chiave | 50/min | Prezzo SOL/USD e prezzo di riserva dei token |
| | `api.jup.ag/price/v2` | ❌ 404 (deprecata) | – | non usata | – |
| **RugCheck** | `/v1/tokens/{mint}/report/summary`, `/v1/tokens/{mint}/report` | ✅ 200 (`samples/rugcheck_*.json`) | non pubblicato | 20/min (1 token ogni 4 s) | Holder principali, numero holder, mint/freeze authority, flag "rugged", rischi |
| **Solana RPC pubblico** | `getSignaturesForAddress`, `getTransaction` (con `maxSupportedTransactionVersion: 1`) | ✅ 200 | 100 richieste/10 s per IP, 40/10 s per metodo | 60/10 s | Copy trading (swap dei wallet pubblici), campionamento FOMO |
| | `getTokenLargestAccounts` | ⚠️ 429 frequente | come sopra | non usato | Holder presi da RugCheck |
| **FOMO** | nessuna API pubblica ufficiale (verificato su doc Bitquery, 29/09/2026) | ⚠️ | – | – | **Alternativa**: campionamento on-chain delle transazioni del signer FOMO `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` via RPC pubblico (`samples/fomo_signer_signatures.json`, `samples/fomo_tx.json`). Dati **parziali/campionati**, non completi. `fomo-api.com` è un servizio di terzi che richiede chiave: **non usato**. |
| **Birdeye** | `public-api.birdeye.so/defi/price` | ❌ 401 senza chiave | richiede API key | disattivato (chiave facoltativa) | – |

## Dati che MEMEBOT ricava
* **Nuovi token**: PumpPortal (secondi) + Dexscreener profiles/boosts (30 s) + GeckoTerminal new_pools (45 s).
* **Mercato**: Dexscreener `tokens/v1` in blocchi da 30 mint ogni 10 s per token attivi e posizioni aperte; fallback Jupiter.
  I token ancora sulla bonding curve Pump.fun possono avere `liquidity = null` su Dexscreener: lo scanner li marca come "dati parziali".
* **Distribuzione holder / sicurezza**: RugCheck (top holder esclusi conti AMM/locker/market, authority, rugged).
* **Copy trading**: swap ricostruiti da `getTransaction` (bilanci token pre/post + lamport SOL) dei wallet **pubblici** inseriti.

## Problemi riscontrati e soluzioni
* `getTransaction` restituiva "Transaction version (1) not supported" → aggiunto `maxSupportedTransactionVersion: 1`.
* Concentrazione holder RugCheck gonfiata dai conti bonding-curve/AMM → esclusi `markets` e `knownAccounts` di tipo AMM/LOCKER.
* Pump.fun 403 → PumpPortal (gratuito) per i lanci in tempo reale.
* PumpPortal è un servizio di terzi non ufficiale: se cade, lo scanner continua con Dexscreener + GeckoTerminal e lo stato "PumpPortal" diventa `error` (evento `scanner_issue` all'app).

## Stato delle fonti a runtime
`GET /api/status` restituisce per ogni fonte: ultimo successo, ultimo errore, numero richieste, 429 ricevuti, latenza.

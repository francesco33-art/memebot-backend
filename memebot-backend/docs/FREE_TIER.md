# MEMEBOT – FREE TRIAL MODE (72 ore, solo servizi gratuiti)

Verifica eseguita il **13/11/2026** leggendo le pagine ufficiali di prezzi/limiti (link sotto).
**Nessun account è stato creato e nessun servizio è stato attivato o deployato.**
I prezzi possono cambiare: ricontrolla i link prima di registrarti.

## 1. Riepilogo verdetti

| Servizio | Piano gratuito reale? | Carta richiesta? | 72 h continue garantite? | Note |
|---|---|---|---|---|
| **Computer tuo (Mac/PC) + Docker/PostgreSQL** | ✅ 100% gratuito | No | ✅ **Sì**, se il computer resta acceso, collegato e non va in stop | Nessuna dipendenza da terzi. Raccomandato per il test di 72 h |
| **Render – Web Service Free** | ✅ | No (senza metodo di pagamento Render sospende invece di addebitare) | ⚠️ **NO, non garantito**: sospensione dopo 15 min senza traffico in ingresso; "può riavviare in qualsiasi momento" | 512 MB RAM, 750 ore/mese, filesystem effimero, 5 GB banda in uscita/mese (Hobby) |
| **Render – Postgres Free** | ✅ | No | ✅ per 72 h (scade **30 giorni** dopo la creazione, poi 14 giorni per l'upgrade, poi cancellazione) | 1 GB, un solo DB Free per workspace, niente backup |
| **Render – Background Worker** | ❌ non gratuito (da 7 $/mese) | – | – | Per questo nel piano gratuito scanner+motore girano **nello stesso processo** dell'API (`RUN_WORKERS=all`) |
| **Neon Postgres Free** | ✅ permanente, no carta | No | ⚠️ a rischio | 100 CU-ore/progetto/mese (72 h × 0,25 CU ≈ 18 CU-h: OK), 1 GB, **5 GB egress/mese**: misurato in locale ≈ 1,45 MB/min letti dal DB ≈ **6,2 GB in 72 h → supera il limite** e Neon sospende il compute. Usabile solo con intervalli più lunghi (vedi §4) |
| **Supabase Free** | ✅ | No | ⚠️ 72 h OK, ma pausa dopo 1 settimana di inattività; **5 GB egress** (stesso problema di Neon) | 500 MB DB, 2 progetti |
| **Railway** | ❌ solo trial una tantum 5 $/30 giorni, poi 1 $/mese | Account non verificato = rete in uscita limitata | ⚠️ Trial non è un piano gratuito: **sconsigliato** | Volumi del trial cancellati alla scadenza. `railway.json` incluso solo per completezza |
| **Fly.io** | ❌ nessun piano gratuito (trial 7 giorni, poi carta) | Sì | – | Non usare |
| **Koyeb** | ❌ nessun compute gratuito in listino (Pro 29 $/mese); solo Postgres "Free 5h" | – | ❌ | Non usare |
| **Oracle Cloud Always Free** | ✅ VM gratuite permanenti | **Sì, verifica carta all'iscrizione** (nessun addebito se non fai upgrade) | ✅ VM sempre accesa (ma istanze inattive possono essere recuperate e la capacità può mancare) | Alternativa cloud 24/7 – serve la tua autorizzazione esplicita per la registrazione con carta |
| **Cloudflare Quick Tunnel** (`trycloudflare.com`) | ✅ senza account | No | ⚠️ "nessuna garanzia di uptime", URL cambia a ogni avvio, max 200 richieste in volo | Per raggiungere il backend del tuo computer da fuori casa |
| **UptimeRobot Free** | ✅ 50 monitor, intervallo 5 min, "No credit card required" | No | – | Ping esterno per tenere sveglio Render Free (soluzione non ufficiale) |
| **Fonti dati** (PumpPortal, Dexscreener, GeckoTerminal, Jupiter lite, RugCheck, RPC Solana pubblico) | ✅ senza chiave | No | ⚠️ servizi pubblici senza SLA; possibili 429/blocchi | Vedi `DATA_SOURCES.md`. Birdeye (chiave) facoltativo e disattivato |
| **iPhone: Apple ID gratuito + Xcode** | ✅ | No | ⚠️ firma valida **7 giorni** (sufficiente per 72 h) | Max 3 app sideload per dispositivo |
| **TestFlight / push remote APNs** | ❌ richiedono Apple Developer Program (99 $/anno) | Sì | – | **Opzionali e a pagamento**: non necessari. L'app usa notifiche locali |

## 2. Stack consigliato (gratuito, nessun costo nascosto)

### Opzione A – CONSIGLIATA per 72 ore garantite: tutto sul tuo computer
* `docker compose up -d --build` (PostgreSQL 16 + backend in un container, `restart: unless-stopped`) oppure `backend/run_local.sh` senza Docker.
* iPhone sulla **stessa rete Wi-Fi**: nell'app → Impostazioni → URL `http://<IP-del-computer>:8000`.
* Impedisci lo stop del computer per 72 h (macOS: `caffeinate -dimsu` in un terminale; Windows: Opzioni risparmio energia → "Mai").
* Fuori casa (facoltativo): `cloudflared tunnel --url http://localhost:8000` → URL `https://xxxx.trycloudflare.com` (nessun account, ma **nessuna garanzia di uptime** e l'URL cambia a ogni riavvio). Imposta `API_TOKEN` perché chiunque abbia l'URL può raggiungere il servizio.
* Costo: 0 €. Rischio: il computer deve restare acceso e online.

### Opzione B – cloud gratuito (Render Free web + Render Free Postgres), con limite segnalato
* `render.yaml` incluso: 1 web service **free** (`RUN_WORKERS=all`, `KEEPALIVE_ENABLED=true`) + 1 Postgres **free**. Nessun worker a pagamento.
* ⚠️ **Render Free NON garantisce 72 h continue**: dopo 15 min senza richieste in ingresso il servizio si sospende e lo scanner si ferma; Render può inoltre riavviarlo in qualsiasi momento. Mitigazioni (non garantite da Render):
  1. `KEEPALIVE_ENABLED=true`: il backend richiama il proprio URL pubblico (`RENDER_EXTERNAL_URL`) ogni 10 minuti;
  2. monitor gratuito esterno (UptimeRobot Free, ogni 5 min) su `https://<tuo-servizio>.onrender.com/api/health`;
  3. app iPhone aperta: il WebSocket invia ping ogni 20 s (conta come traffico in ingresso).
  Dopo un riavvio lo stato (posizioni, saldo virtuale, storico) **resta nel Postgres** e i worker ripartono da soli.
* Il DB Free di Render **scade dopo 30 giorni**: per 72 h va bene. Il traffico web→DB è sulla rete privata Render, quindi non consuma i 5 GB di banda in uscita.
* Banda in uscita 5 GB/mese: snapshot dashboard ≈ 8,7 KB ogni 5 s per app connessa ≈ 450 MB in 72 h → entro il limite.
* RAM: il processo misurato in locale usa ≈ 100 MB RSS (limite Free 512 MB).
* Se la banda si esaurisce e non hai una carta registrata, Render **sospende** i servizi gratuiti (non addebita).

### Opzione C – 24/7 vera nel cloud: Oracle Cloud Always Free ✅ SCELTA PER IL TEST – guida: [`ORACLE.md`](ORACLE.md)
* VM Always Free (AMD E2.1.Micro o Ampere A1) + `docker compose`. Sempre accesa, nessuna sospensione per inattività del traffico.
* ⚠️ L'iscrizione richiede **verifica con carta di credito** (nessun addebito se resti sui servizi Always Free); Oracle può recuperare istanze "idle" (valutazione su 7 giorni: nessun rischio per 72 h) e a volte non c'è capacità A1 disponibile. Nessun account creato: l'iscrizione la fai tu seguendo `ORACLE.md`.

### Database alternativi (Neon / Supabase)
Funzionano con `DATABASE_URL=postgresql://...?sslmode=require`, ma il limite di **5 GB/mese di traffico** è a rischio in 72 h con gli intervalli predefiniti (misura locale ≈ 6,2 GB/72 h). Se li usi, imposta intervalli più lunghi:
`ENRICH_INTERVAL=30 ENGINE_INTERVAL=6 GECKOTERMINAL_INTERVAL=90 DEXSCREENER_DISCOVERY_INTERVAL=60`
(riduzione stimata, **non misurata**) e controlla il consumo nella console. Neon a fine quota sospende il compute (non addebita) fino al mese successivo.

## 3. Cosa NON è stato fatto (per tua sicurezza)
* Nessuna registrazione, nessuna carta, nessun trial con rinnovo automatico, nessun deploy.
* Nessuna API premium obbligatoria. Nessuna chiave privata, seed phrase o firma: tutto il trading è **simulato** (`DEMO_ONLY` forzato, invio transazioni bloccato dal DEMO guard).

## 4. Misure di riferimento (box di sviluppo, 13/11/2026, Europe/Rome)
* Database dopo ~16 min di scanner live: 11 MB (price_snapshots 4.753 righe, tokens 424). Eventi più vecchi di 48 h vengono eliminati dal job di manutenzione.
* Traffico DB→app: ≈ 1,45 MB/min (misurato con `ss` sulle connessioni a PostgreSQL in 60 s).
* Risposta `/api/dashboard`: 8.721 byte.

## 5. Fonti ufficiali consultate
* Render Free: https://render.com/docs/free – Render pricing: https://render.com/pricing
* Railway free trial: https://docs.railway.com/reference/pricing/free-trial
* Neon pricing: https://neon.com/pricing
* Supabase pricing: https://supabase.com/pricing
* Koyeb pricing: https://www.koyeb.com/pricing
* Fly.io pricing: https://fly.io/docs/about/pricing/
* Oracle Always Free: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
* Cloudflare Quick Tunnels: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
* UptimeRobot pricing: https://uptimerobot.com/pricing/
* Apple – firma con account gratuito / Developer Program: https://developer.apple.com/support/compare-memberships/

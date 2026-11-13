"""Configurazione MEMEBOT (variabili d'ambiente, mai credenziali nel codice dell'app iOS)."""
from __future__ import annotations
from typing import Optional

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Modalità: questo valore NON è modificabile. MEMEBOT è solo DEMO. ---
    demo_only: bool = True

    database_url: str = "sqlite:///./memebot.db"
    api_token: Optional[str] = None  # se impostato, richiesto come header X-API-Key / ?token= sul WebSocket
    run_workers: str = ""  # "" | "all" | "scanner" | "engine" -> avvia worker nello stesso processo dell'API
    log_level: str = "INFO"
    log_json: bool = True

    # Fonti dati (tutte pubbliche, senza chiave)
    dexscreener_base: str = "https://api.dexscreener.com"
    geckoterminal_base: str = "https://api.geckoterminal.com/api/v2"
    jupiter_price_base: str = "https://lite-api.jup.ag/price/v3"
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    rugcheck_base: str = "https://api.rugcheck.xyz/v1"
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"  # solo lettura
    birdeye_api_key: Optional[str] = None  # opzionale, non usato se assente

    enable_pumpportal: bool = True
    enable_dexscreener: bool = True
    enable_geckoterminal: bool = True
    enable_rugcheck: bool = True
    enable_fomo: bool = True

    # Intervalli scanner (secondi)
    dexscreener_discovery_interval: int = 30
    enrich_interval: int = 10
    geckoterminal_interval: int = 45
    sol_price_interval: int = 30
    rugcheck_interval: float = 4.0
    fomo_interval: int = 60
    fomo_sample_tx: int = 3
    token_monitor_hours: int = 6
    max_monitored_tokens: int = 400

    # Motore
    engine_interval: float = 3.0
    copy_poll_interval: int = 20
    equity_snapshot_interval: int = 60
    default_initial_capital_sol: float = 2.0
    max_price_age_sec: int = 180

    # Keep-alive opzionale per hosting gratuiti che vanno in sospensione (es. Render Free: 15 min senza traffico).
    # Se KEEPALIVE_ENABLED=true e KEEPALIVE_URL è vuoto, usa RENDER_EXTERNAL_URL (impostata da Render).
    keepalive_enabled: bool = False
    keepalive_url: Optional[str] = None
    render_external_url: Optional[str] = None
    keepalive_interval: int = 600

    # Notifiche push lato server (APNs) - opzionali, solo env vars
    apns_key_id: Optional[str] = None
    apns_team_id: Optional[str] = None
    apns_bundle_id: Optional[str] = None
    apns_private_key_path: Optional[str] = None  # chiave .p8 di Apple Push (NON è una chiave wallet)
    apns_use_sandbox: bool = True


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Vincolo assoluto: qualsiasi tentativo di disattivare la modalità demo viene ignorato.
    s.demo_only = True
    return s

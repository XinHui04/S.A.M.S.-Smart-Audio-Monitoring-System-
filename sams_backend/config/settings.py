"""
config/settings.py
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Server
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_env:  str = "development"

    # Module 1 — Speech Detection & Audio Capture
    audio_storage_dir:    str   = "./audio_storage"
    vad_energy_threshold: float = 0.01
    max_audio_duration:   float = 10.0

    # Module 2 — Cloud Processing & AI Analysis
    # STT: Groq API (free — https://console.groq.com)
    groq_api_key: str = ""

    # NLP: RoBERTa offensive classifier (free, runs locally). Labels: offensive / non-offensive.
    # NOTE: the previously-configured "cardiffnlp/twitter-xlm-roberta-base-offensive" does not
    # exist on HuggingFace (404/401); this is the real Cardiff offensive model. The Malay/Manglish
    # keyword layer in nlp_service.py supplements it for local slang.
    nlp_model:              str   = "cardiffnlp/twitter-roberta-base-offensive"
    threat_score_threshold: float = 0.75

    # Module 3 — Reporting & Analytics
    # Database: set DATABASE_URL to a Supabase Postgres connection string to use the cloud.
    # When empty, the app falls back to the local SQLite file at SQLITE_DB_PATH (dev/offline/tests).
    database_url:   str = ""
    sqlite_db_path: str = "./sams.db"

    # Audio object storage: "local" keeps clips on disk (default); "supabase" uploads to a bucket.
    storage_backend:           str  = "local"          # "local" | "supabase"
    supabase_url:              str  = ""               # https://<ref>.supabase.co
    supabase_service_key:      str  = ""               # service_role key — server-side ONLY, keep secret
    supabase_bucket:           str  = "audio-clips"
    delete_local_after_upload: bool = True             # privacy: remove the local working copy after upload

    # Module 4 — Main Computer Monitoring System
    websocket_ping_interval: int = 30

    # Module 4 — MQTT publisher (real-time alert fan-out; Figs 4.1/4.2)
    # Opt-in: leave mqtt_enabled False to run without a broker.
    mqtt_enabled:     bool = False
    mqtt_broker_host: str  = "localhost"
    mqtt_broker_port: int  = 1883
    mqtt_topic:       str  = "sams/alerts"
    mqtt_username:    str  = ""
    mqtt_password:    str  = ""
    mqtt_use_tls:     bool = False
    mqtt_qos:         int  = 1

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()

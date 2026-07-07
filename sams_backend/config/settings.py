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

    # NLP: multilingual XLM-RoBERTa toxicity classifier (free, runs locally).
    # Labels: toxic / neutral. Despite the repo name, the checkpoint is XLM-R
    # BASE architecture (~278M params, ~1.1 GB fp32) — verified from its config.
    # Adopted after measured validation (see FYP report): equal English toxic
    # accuracy vs the English-only model, far better Malay/Manglish detection,
    # zero benign false positives on the eval set.
    # Fallback (English-only) option: "cardiffnlp/twitter-roberta-base-offensive"
    # — set NLP_MODEL in .env to switch; nlp_service.py handles both label schemes.
    # NOTE: "cardiffnlp/twitter-xlm-roberta-base-offensive" does NOT exist on
    # HuggingFace (404) — do not configure it.
    nlp_model:              str   = "textdetox/xlmr-large-toxicity-classifier"
    threat_score_threshold: float = 0.75

    # SER: Speech Emotion Recognition (report §2.1.3) — wav2vec2 audio classifier
    # (SUPERB/IEMOCAP, 4 classes: angry/happy/neutral/sad, ~380 MB, runs locally).
    # When a negative emotion (angry/fearful) is detected with confidence >=
    # ser_min_confidence, the NLP threat score is boosted by ser_boost (cap 1.0)
    # before the alert-threshold comparison. SER failure never blocks the pipeline.
    ser_enabled:        bool  = True
    ser_model:          str   = "superb/wav2vec2-base-superb-er"
    ser_boost:          float = 0.15
    ser_min_confidence: float = 0.60

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

    # Auth — JWT login for dashboard/staff endpoints (FR23)
    jwt_secret_key:              str = ""       # REQUIRED for auth — set in .env, never commit
    jwt_algorithm:               str = "HS256"
    access_token_expire_minutes: int = 480
    device_api_key:              str = ""       # opt-in — when set, ESP32 ingestion endpoints require X-API-Key

    # CORS — comma-separated list of allowed origins for the dashboard/PWA.
    # In development ("app_env=development") a wildcard is also allowed so the
    # LAN demo works when the dashboard is opened from any host; in production
    # set this explicitly (e.g. "https://sams.school.edu.my").
    cors_allow_origins: str = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500"

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

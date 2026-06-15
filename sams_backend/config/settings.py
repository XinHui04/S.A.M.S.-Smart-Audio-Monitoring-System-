"""
config/settings.py
Configuration for Lim Xin Hui's 4 modules.
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

    # NLP: XLM-RoBERTa (free, runs locally)
    nlp_model:              str   = "cardiffnlp/twitter-xlm-roberta-base-offensive"
    threat_score_threshold: float = 0.75

    # Module 3 — Reporting & Analytics
    sqlite_db_path: str = "./sams.db"

    # Module 4 — Main Computer Monitoring System
    websocket_ping_interval: int = 30

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()

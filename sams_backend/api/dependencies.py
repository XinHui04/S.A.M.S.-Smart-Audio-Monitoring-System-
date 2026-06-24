"""
api/dependencies.py
Singleton wiring — all 4 modules instantiated once at startup.
"""
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import create_db_engine, get_session_factory
from services.audio_capture_service import AudioCaptureService
from services.stt_service import STTService
from services.nlp_service import NLPService
from services.processing_pipeline import ProcessingPipeline
from services.websocket_manager import WebSocketManager
from services.mqtt_service import MqttService
from services.storage_service import AudioStorageService

cfg = get_settings()

# ── Singletons ────────────────────────────────────────────────────────────────
_engine         = create_db_engine(database_url=cfg.database_url, sqlite_path=cfg.sqlite_db_path)
_SessionFactory = get_session_factory(_engine)

_audio_capture  = AudioCaptureService(
    storage_dir       = cfg.audio_storage_dir,
    vad_threshold     = cfg.vad_energy_threshold,
    max_duration_secs = cfg.max_audio_duration,
)
_audio_storage  = AudioStorageService(
    backend      = cfg.storage_backend,
    storage_dir  = cfg.audio_storage_dir,
    supabase_url = cfg.supabase_url,
    supabase_key = cfg.supabase_service_key,
    bucket       = cfg.supabase_bucket,
)
_stt        = STTService(api_key=cfg.groq_api_key)   # Groq free API
_nlp        = NLPService(model_name=cfg.nlp_model, threshold=cfg.threat_score_threshold)
_ws_manager = WebSocketManager()
_mqtt       = MqttService(
    enabled  = cfg.mqtt_enabled,
    host     = cfg.mqtt_broker_host,
    port     = cfg.mqtt_broker_port,
    topic    = cfg.mqtt_topic,
    username = cfg.mqtt_username,
    password = cfg.mqtt_password,
    use_tls  = cfg.mqtt_use_tls,
    qos      = cfg.mqtt_qos,
)

_pipeline = ProcessingPipeline(
    audio_capture = _audio_capture,
    stt           = _stt,
    nlp           = _nlp,
    websocket_mgr = _ws_manager,
    mqtt          = _mqtt,
    threshold     = cfg.threat_score_threshold,
    audio_storage = _audio_storage,
    delete_local_after_upload = cfg.delete_local_after_upload,
)

# ── Dependency functions ──────────────────────────────────────────────────────

def get_db():
    db = _SessionFactory()
    try:
        yield db
    finally:
        db.close()

def get_pipeline() -> ProcessingPipeline:
    return _pipeline

def get_ws_manager() -> WebSocketManager:
    return _ws_manager

def get_mqtt() -> MqttService:
    return _mqtt

def get_audio_capture() -> AudioCaptureService:
    return _audio_capture

def get_audio_storage() -> AudioStorageService:
    return _audio_storage

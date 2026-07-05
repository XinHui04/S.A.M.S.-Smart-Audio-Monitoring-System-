"""
api/dependencies.py
Singleton wiring — all 4 modules instantiated once at startup.
Also hosts the auth dependencies (FR23): JWT bearer for staff endpoints,
X-API-Key check for device ingestion.
"""
import hmac
import logging
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import User, create_db_engine, get_session_factory
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
    # storage_dir       = cfg.audio_storage_dir,
    # vad_threshold     = cfg.vad_energy_threshold,
    # max_duration_secs = cfg.max_audio_duration,
    supabase_url = cfg.supabase_url,
    supabase_key = cfg.supabase_service_key,
    bucket_name  = cfg.supabase_bucket,
)
_audio_storage  = AudioStorageService(
    backend      = cfg.storage_backend,
    storage_dir  = cfg.audio_storage_dir,
    supabase_url = cfg.supabase_url,
    supabase_key = cfg.supabase_service_key,
    bucket_name  = cfg.supabase_bucket,
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


# ── Auth dependencies (FR23) ──────────────────────────────────────────────────

_logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)   # we raise our own 401 (consistent detail)

_warned_auth_unconfigured = False


def _warn_unconfigured_once():
    """Log (once) that auth is disabled because JWT_SECRET_KEY is not set."""
    global _warned_auth_unconfigured
    if not _warned_auth_unconfigured:
        _logger.warning(
            "JWT_SECRET_KEY is not set — auth is DISABLED and all protected "
            "endpoints allow anonymous access. Set JWT_SECRET_KEY in .env."
        )
        _warned_auth_unconfigured = True


def resolve_token_user(token: str, db: Session) -> Optional[User]:
    """
    Decode a JWT and load the matching User row, or return None if the token
    is missing/expired/invalid or the user no longer exists.
    Shared by get_current_user (HTTP) and the /ws/dashboard handler in main.py.
    Role/identity always come from the DB row — never trusted from the claims.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, cfg.jwt_secret_key, algorithms=[cfg.jwt_algorithm])
    except jwt.InvalidTokenError:          # covers ExpiredSignatureError too
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    return db.query(User).filter(User.user_id == user_id).first()


def _authenticate(token: Optional[str], db: Session) -> Optional[User]:
    """Validate a bearer token and return the DB User. Raises 401 on failure."""
    # Unconfigured dev setup: JWT_SECRET_KEY empty → allow anonymous access
    # (returns None) so the demo still works before .env is configured.
    # Endpoints must therefore tolerate user being None.
    if not cfg.jwt_secret_key:
        _warn_unconfigured_once()
        return None

    if not token:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, cfg.jwt_secret_key, algorithms=[cfg.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired", headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})

    user = db.query(User).filter(User.user_id == payload.get("sub")).first()
    if not user:   # account deleted since the token was issued
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return user


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Require a valid Bearer JWT; returns the current User row (role from DB)."""
    return _authenticate(credentials.credentials if credentials else None, db)


def get_current_user_query_ok(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    token: Optional[str] = Query(default=None, description="JWT fallback for <audio> tags (no headers)"),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Same as get_current_user, but also accepts ?token=<jwt> as a fallback for
    the dashboard <audio> element, which cannot send Authorization headers.
    The Authorization header takes precedence when both are present.
    """
    return _authenticate(credentials.credentials if credentials else token, db)


def require_admin(user: Optional[User] = Depends(get_current_user)) -> Optional[User]:
    """Restrict an endpoint to admins (403 otherwise)."""
    if user is not None and user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def verify_device_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """
    Device ingestion guard (ESP32 → cloud). Opt-in: when DEVICE_API_KEY is
    empty the endpoints stay open (demo mode); when set, the X-API-Key header
    must match (constant-time compare — no timing side channel).
    """
    if not cfg.device_api_key:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, cfg.device_api_key):
        raise HTTPException(401, "Invalid device API key")

"""
api/dependencies.py
Singleton wiring — all 4 modules instantiated once at startup.
Also hosts the auth dependencies (FR23): JWT bearer for staff endpoints,
X-API-Key check for device ingestion.
"""
import hashlib
import hmac
import logging
from typing import Optional

import jwt
from fastapi import Depends, Form, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import (
    DeviceCredential, User, create_db_engine, get_session_factory,
)
from services.audio_capture_service import AudioCaptureService
from services.stt_service import STTService
from services.nlp_service import NLPService
from services.ser_service import SERService
from services.processing_pipeline import ProcessingPipeline
from services.websocket_manager import WebSocketManager
from services.mqtt_service import MqttService
from services.storage_service import AudioStorageService
from services.push_service import PushService

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
_ser        = SERService(model_name=cfg.ser_model) if cfg.ser_enabled else None
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
_push       = PushService(
    vapid_public_key  = cfg.vapid_public_key,
    vapid_private_key = cfg.vapid_private_key,
    vapid_subject     = cfg.vapid_subject,
    session_factory   = _SessionFactory,
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
    ser                = _ser,
    ser_boost          = cfg.ser_boost,
    ser_min_confidence = cfg.ser_min_confidence,
    push               = _push,
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

def get_push() -> PushService:
    return _push

def get_audio_capture() -> AudioCaptureService:
    return _audio_capture

def get_audio_storage() -> AudioStorageService:
    return _audio_storage


# ── Auth dependencies (FR23) ──────────────────────────────────────────────────

_logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)   # we raise our own 401 (consistent detail)

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
    # Fail closed: without a signing secret no token can be validated.
    # main.py refuses to start in this state; this guard covers direct
    # imports (e.g. test harnesses that skip the lifespan).
    if not cfg.jwt_secret_key:
        raise HTTPException(503, "Authentication not configured")

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
    """Restrict an endpoint to admins (403 otherwise); never passes an
    unauthenticated user."""
    if user is None or user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def verify_device_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """
    Global device ingestion guard (ESP32 → cloud). Opt-in: when DEVICE_API_KEY
    is empty the endpoints stay open (demo mode); when set, the X-API-Key header
    must match (constant-time compare — no timing side channel).

    Header-only: it cannot see a per-device identity, so it is used where the
    device_id is not yet known (the Supabase Storage webhook). Endpoints that
    know the device_id use check_device_key / verify_device_key_form instead.
    """
    if not cfg.device_api_key:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, cfg.device_api_key):
        raise HTTPException(401, "Invalid device API key")


def check_device_key(device_id: str, x_api_key: Optional[str], db: Session) -> None:
    """
    Per-device ingestion guard (FR25). Authorizes a request for `device_id`,
    raising HTTPException(401) with an identical, non-revealing detail otherwise.

    Rules:
      • Device WITH a DeviceCredential row → fail-closed: an X-API-Key header is
        REQUIRED and its SHA-256 digest must match the stored hash
        (constant-time compare).
      • Device WITHOUT a credential row → fall back to the global DEVICE_API_KEY
        behavior (fail-open demo continuity): require the global key when it is
        configured, else allow.

    The error never discloses whether the device is enrolled.
    """
    cred = (
        db.query(DeviceCredential)
          .filter(DeviceCredential.device_id == device_id)
          .first()
    )
    if cred is not None:
        if not x_api_key:
            raise HTTPException(401, "Invalid device API key")
        presented = hashlib.sha256(x_api_key.encode()).hexdigest()
        if not hmac.compare_digest(presented, cred.key_hash):
            raise HTTPException(401, "Invalid device API key")
        return

    # Un-enrolled device → global-key fallback.
    if not cfg.device_api_key:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, cfg.device_api_key):
        raise HTTPException(401, "Invalid device API key")


def verify_device_key_form(
    device_id: str = Form(...),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
):
    """
    Device-aware ingestion guard for form endpoints whose device_id lives in the
    request body (POST /api/events/audio). Declared as a dependency that reads
    device_id as a Form field so the auth check runs DURING dependency
    resolution — before the endpoint's other required Form fields are validated.
    This preserves the 401-before-422 contract (an unauthenticated caller is
    rejected before body-shape errors leak) while still enforcing the per-device
    key via check_device_key.
    """
    check_device_key(device_id, x_api_key, db)

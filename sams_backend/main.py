"""
main.py — S.A.M.S. Cloud Backend

Modules covered:
  1. Speech Detection & Audio Capture  → POST /api/events/audio
  2. Cloud Processing & AI Analysis    → processing_pipeline.py
  3. Reporting & Analytics             → GET  /api/analytics/
  4. Main Computer Monitoring System   → GET  /api/alerts/ + WS /ws/dashboard

Run:  uvicorn main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config.settings import get_settings
from utils.rate_limit import limiter
from api.events    import router as events_router
from api.alerts    import router as alerts_router
from api.analytics import router as analytics_router
from api.auth      import router as auth_router
from api.admin     import router as admin_router
from api.reports   import router as reports_router
from api.devices   import router as devices_router
from api.push      import router as push_router
from api.checkin   import router as checkin_router
from api.dependencies import get_ws_manager, get_mqtt, resolve_token_user, _SessionFactory
from models.database import StaffLocation

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger   = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== S.A.M.S. Cloud Backend starting ===")
    if not settings.jwt_secret_key:
        logger.critical(
            "JWT_SECRET_KEY is not set — refusing to start. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"\n'
            "and set it in sams_backend/.env (see .env.example)."
        )
        raise RuntimeError("JWT_SECRET_KEY is not configured")
    logger.info(f"STT  : Groq Whisper Large v3 ({'API key set' if settings.groq_api_key else 'NO API KEY — transcription disabled'})")
    logger.info(f"NLP  : {settings.nlp_model}")
    logger.info(f"Threshold : {settings.threat_score_threshold}")
    logger.info(f"MQTT : {'enabled — ' + settings.mqtt_broker_host + ':' + str(settings.mqtt_broker_port) + ' topic ' + settings.mqtt_topic if settings.mqtt_enabled else 'disabled'}")
    get_mqtt().connect()
    yield
    get_mqtt().disconnect()
    logger.info("=== S.A.M.S. Cloud Backend stopped ===")


app = FastAPI(
    title       = "S.A.M.S. Cloud Backend",
    description = "Lim Xin Hui — TARUMT FYP 2025/26",
    version     = "1.0.0",
    lifespan    = lifespan,
)

# ── Rate limiting (slowapi) ───────────────────────────────────────────────────
# Decorator-based only (@limiter.limit on selected endpoints) — no global
# middleware. The default 429 handler returns a generic message (no internals).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Validation errors — redact credential material ────────────────────────────
# FastAPI's default 422 body echoes each field's submitted `input`. For password
# fields that would leak the plaintext (login, admin staff creation), so we strip
# `input` (and any `ctx`) from errors whose location includes a password field.
_SENSITIVE_FIELDS = {"password"}

@app.exception_handler(RequestValidationError)
async def _redact_validation_errors(request: Request, exc: RequestValidationError):
    cleaned = []
    for err in exc.errors():
        loc = err.get("loc", ())
        if any(part in _SENSITIVE_FIELDS for part in loc):
            err = {k: v for k, v in err.items() if k not in ("input", "ctx")}
        cleaned.append(err)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(cleaned)})

# ── CORS ──────────────────────────────────────────────────────────────────────
# Explicit allow-list from settings. In development we also accept any origin
# (via allow_origin_regex, which — unlike allow_origins=["*"] — is compatible
# with allow_credentials=True) so the LAN demo works from any host/IP.
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
_cors_kwargs = dict(
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)
if settings.app_env == "development":
    app.add_middleware(CORSMiddleware, allow_origin_regex=".*", **_cors_kwargs)
    logger.info("CORS: development mode — all origins allowed")
else:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, **_cors_kwargs)
    logger.info(f"CORS: production mode — allowed origins: {_cors_origins}")

# ── REST routes ───────────────────────────────────────────────────────────────
app.include_router(events_router)
app.include_router(alerts_router)
app.include_router(analytics_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(reports_router)
app.include_router(devices_router)
app.include_router(push_router)
app.include_router(checkin_router)


# ── MODULE 4: WebSocket endpoint for dashboard real-time feed ─────────────────
@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """
    Dashboard browser connects here to receive live alerts instantly.
    No polling needed — alerts are pushed the moment they fire.

    Auth (FR23): the browser passes its JWT as ?token=<jwt> (WebSocket clients
    can't set Authorization headers). Invalid/missing token → closed with 4401
    before the handshake is accepted. The server refuses to start without
    JWT_SECRET_KEY (see lifespan), so this token check always applies.
    """
    user_info = None   # None = unrestricted (FR16 fail-open, dev mode)
    token = websocket.query_params.get("token")
    db = _SessionFactory()
    try:
        user = resolve_token_user(token, db)
        if user is not None:
            # FR16: load this user's location assignments for alert routing.
            # No rows / empty list = unrestricted (fail-open — missing
            # config must never hide a safety incident).
            location_ids = [
                row.location_id
                for row in db.query(StaffLocation)
                             .filter(StaffLocation.user_id == user.user_id)
                             .all()
            ]
            user_info = {
                "user_id":      user.user_id,
                "role":         user.role,
                "location_ids": location_ids,
            }
    finally:
        db.close()
    if user is None:
        await websocket.close(code=4401)   # reject before accepting
        return

    mgr = get_ws_manager()
    await mgr.connect(websocket, user_info=user_info)
    try:
        while True:
            # Keep connection alive; dashboard can send "ping" to check
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        mgr.disconnect(websocket)


@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "system": "S.A.M.S.", "version": "1.0.0"}


# ── Teacher PWA (mobile) ──────────────────────────────────────────────────────
# Serve the companion teacher app as static files so it is a real, installable
# PWA over HTTP (service workers require http/https, not file://) and shares the
# backend's origin (no CORS, same WebSocket host). Open on a phone at:
#   http://<server-lan-ip>:8000/m/
# Mounted last so it never shadows the /api or /ws routes above.
_mobile_dir = Path(__file__).resolve().parent.parent / "sams_mobile"
if _mobile_dir.is_dir():
    app.mount("/m", StaticFiles(directory=str(_mobile_dir), html=True), name="mobile")
    logger.info(f"Teacher PWA served at /m/  (dir: {_mobile_dir})")
else:
    logger.warning(f"Teacher PWA dir not found: {_mobile_dir} — /m/ not mounted")

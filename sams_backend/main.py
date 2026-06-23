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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import get_settings
from api.events    import router as events_router
from api.alerts    import router as alerts_router
from api.analytics import router as analytics_router
from api.dependencies import get_ws_manager, get_mqtt

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger   = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== S.A.M.S. Cloud Backend starting ===")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── REST routes ───────────────────────────────────────────────────────────────
app.include_router(events_router)
app.include_router(alerts_router)
app.include_router(analytics_router)


# ── MODULE 4: WebSocket endpoint for dashboard real-time feed ─────────────────
@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """
    Dashboard browser connects here to receive live alerts instantly.
    No polling needed — alerts are pushed the moment they fire.
    """
    mgr = get_ws_manager()
    await mgr.connect(websocket)
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

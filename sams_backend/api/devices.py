"""
api/devices.py
═══════════════════════════════════════════════════════
FR25 — per-device authentication (issue/rotate keys lives in api/admin.py)
FR29 — device registry + liveness (heartbeat)
═══════════════════════════════════════════════════════

Staff (JWT) can list every device with its derived online/offline status;
devices (X-API-Key, no JWT) ping POST /heartbeat to prove they are alive.

Status is DERIVED, never trusted from the wire: a device is "online" only when
its last heartbeat/ingestion is within DEVICE_OFFLINE_AFTER_SECONDS of now.
When no heartbeat row exists we fall back to the stored Device.status column.
"""
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import (
    Device, DeviceHeartbeat, DeviceCredential, Location, LocationPosition, User,
)
from api.dependencies import get_db, get_current_user, check_device_key
from utils.rate_limit import limiter

router = APIRouter(prefix="/api/devices", tags=["Devices — FR25/FR29"])
logger = logging.getLogger(__name__)
cfg = get_settings()

# Device IDs are echoed into logs and used as object-key/DB-key material, so
# restrict them to a safe charset (blocks log-injection / control chars) and a
# sane length. Mirrors models.schemas.DeviceCreate's pattern.
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DEVICE_ID_MAXLEN = 64


def validate_device_id(device_id: Optional[str]) -> str:
    """Return a trusted device_id or raise HTTPException(400). Never logs the
    raw value (it is untrusted at this point)."""
    if not device_id or not isinstance(device_id, str):
        raise HTTPException(400, "device_id is required")
    if len(device_id) > _DEVICE_ID_MAXLEN or not _DEVICE_ID_RE.match(device_id):
        raise HTTPException(400, "Invalid device_id")
    return device_id


def touch_device_heartbeat(db: Session, device_id: str) -> datetime:
    """Upsert this device's liveness marker to now (FR29). Shared by the
    heartbeat endpoint and the audio-ingestion path. Caller is responsible for
    having validated/ensured the device_id."""
    now = datetime.utcnow()
    hb = (
        db.query(DeviceHeartbeat)
          .filter(DeviceHeartbeat.device_id == device_id)
          .first()
    )
    if hb is None:
        db.add(DeviceHeartbeat(device_id=device_id, last_seen=now))
    else:
        hb.last_seen = now
    db.commit()
    return now


@router.get("/", summary="List all devices with derived online/offline status")
async def list_devices(
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    """Staff view of the device registry (FR29). Status is derived from the
    heartbeat freshness window; the stored key hash is NEVER returned."""
    now       = datetime.utcnow()
    threshold = cfg.device_offline_after_seconds

    # Preload the join tables into dicts (single query each) — avoids N+1.
    locations  = {l.location_id: l for l in db.query(Location).all()}
    heartbeats = {h.device_id: h for h in db.query(DeviceHeartbeat).all()}
    positions  = {p.location_id: p for p in db.query(LocationPosition).all()}
    enrolled   = {c.device_id for c in db.query(DeviceCredential).all()}

    out = []
    for d in db.query(Device).all():
        loc = locations.get(d.location_id)
        pos = positions.get(d.location_id)
        hb  = heartbeats.get(d.device_id)

        if hb is not None:
            last_seen     = hb.last_seen
            online        = (now - last_seen).total_seconds() <= threshold
            status        = "online" if online else "offline"
            status_source = "heartbeat"
        else:
            last_seen     = None
            status        = d.status
            status_source = "stored"

        out.append({
            "device_id":      d.device_id,
            "location_id":    d.location_id,
            "location_name":  loc.location_name if loc else None,
            "map_x":          pos.map_x if pos else None,
            "map_y":          pos.map_y if pos else None,
            "stored_status":  d.status,
            "last_seen":      last_seen.isoformat() if last_seen else None,
            "status_source":  status_source,
            "status":         status,
            "has_credential": d.device_id in enrolled,
        })

    return {"devices": out, "offline_after_seconds": threshold}


@router.post("/heartbeat", summary="[ESP32] Report device liveness")
@limiter.limit("60/minute")   # abuse protection — per client IP
async def device_heartbeat(
    request: Request,
    # device_id arrives in the BODY (form or JSON), so the per-device key check
    # is done in-body via check_device_key (which also applies the global
    # DEVICE_API_KEY fallback) — NOT stacked with the header-only
    # verify_device_key dependency, which would reject a valid per-device key
    # whenever a global key is also configured.
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
):
    """A device pings this on a timer. Accepts form-encoded or JSON body with a
    single `device_id`. Unknown devices are rejected (404) — a bare heartbeat
    never auto-registers a device (that stays in the audio-ingestion path)."""
    # ── Extract device_id from form OR JSON ──────────────────────────────────
    content_type = request.headers.get("content-type", "")
    device_id: Optional[str] = None
    try:
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, dict):
                device_id = data.get("device_id")
        else:
            form = await request.form()
            device_id = form.get("device_id")
    except Exception:
        raise HTTPException(400, "Malformed request body")

    device_id = validate_device_id(device_id)

    # ── Per-device auth (FR25) — enrolled devices must present their key ──────
    check_device_key(device_id, x_api_key, db)

    # ── Known devices only — no auto-register from a bare heartbeat ───────────
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(404, "Unknown device")

    last_seen = touch_device_heartbeat(db, device_id)
    logger.info(f"FR29: heartbeat from device {device_id}")
    return {"status": "ok", "device_id": device_id, "last_seen": last_seen.isoformat()}

"""
api/checkin.py
═══════════════════════════════════════════════════════
FR30 — Staff zone check-in
═══════════════════════════════════════════════════════
Endpoints the teacher PWA calls so a staff member can voluntarily "check in"
to whichever school zone they are currently in:

  GET    /api/staff/checkin → the caller's current check-in (or null) + zone list
  PUT    /api/staff/checkin → check into a zone (upsert, overwrites any prior row)
  DELETE /api/staff/checkin → check out (removes the row)

The nearest-staff ranking (utils/nearest_staff.py) prefers a FRESH check-in
over the caller's static StaffLocation assignments; a check-in older than
settings.checkin_ttl_seconds is treated as stale (ignored for ranking, but
still returned here with "expired": true so the UI can grey it out).

Every endpoint requires a valid staff/admin JWT. Security notes:
  - location_id is validated against the Location table (400 if unknown).
  - GET only ever returns the CALLER's own check-in — no cross-user exposure.
  - One row per user (primary key), so re-checking in overwrites in place —
    no history, no GPS, zone-level only (privacy by design).
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import Location, StaffCheckin, User
from models.schemas import CheckinRequest
from api.dependencies import get_db, get_current_user

router = APIRouter(prefix="/api/staff/checkin", tags=["Check-in — FR30"])
logger = logging.getLogger(__name__)


def _serialize(checkin: StaffCheckin, location_name: str, ttl_seconds: int) -> dict:
    expired = (datetime.utcnow() - checkin.checked_in_at) > timedelta(seconds=ttl_seconds)
    return {
        "location_id":   checkin.location_id,
        "location_name": location_name,
        "checked_in_at": checkin.checked_in_at,
        "expired":       expired,
    }


@router.get("", summary="Get the caller's current zone check-in")
async def get_checkin(
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    if user is None:
        raise HTTPException(503, "Authentication not configured")

    cfg = get_settings()

    checkin_out = None
    row = (
        db.query(StaffCheckin)
          .filter(StaffCheckin.user_id == user.user_id)
          .first()
    )
    if row is not None:
        location = db.query(Location).filter(Location.location_id == row.location_id).first()
        location_name = location.location_name if location else None
        checkin_out = _serialize(row, location_name, cfg.checkin_ttl_seconds)

    locations = (
        db.query(Location)
          .order_by(Location.location_name)
          .all()
    )

    return {
        "checkin": checkin_out,
        "locations": [
            {"location_id": loc.location_id, "location_name": loc.location_name}
            for loc in locations
        ],
        "ttl_seconds": cfg.checkin_ttl_seconds,
    }


@router.put("", summary="Check into a zone (upserts the caller's check-in)")
async def put_checkin(
    body: CheckinRequest,
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    if user is None:
        raise HTTPException(503, "Authentication not configured")

    location = db.query(Location).filter(Location.location_id == body.location_id).first()
    if location is None:
        raise HTTPException(400, "Unknown location_id")

    cfg = get_settings()
    now = datetime.utcnow()

    row = (
        db.query(StaffCheckin)
          .filter(StaffCheckin.user_id == user.user_id)
          .first()
    )
    if row is not None:
        row.location_id   = body.location_id
        row.checked_in_at = now
    else:
        row = StaffCheckin(user_id=user.user_id, location_id=body.location_id, checked_in_at=now)
        db.add(row)
    db.commit()
    db.refresh(row)

    return _serialize(row, location.location_name, cfg.checkin_ttl_seconds)


@router.delete("", summary="Check out (remove the caller's check-in)")
async def delete_checkin(
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    if user is None:
        raise HTTPException(503, "Authentication not configured")

    row = (
        db.query(StaffCheckin)
          .filter(StaffCheckin.user_id == user.user_id)
          .first()
    )
    if row is None:
        raise HTTPException(404, "No active check-in")

    db.delete(row)
    db.commit()
    return {"status": "checked_out"}

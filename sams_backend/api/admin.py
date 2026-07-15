"""
api/admin.py
═══════════════════════════════════════════════════════
FR16: Alert routing administration — staff ↔ location assignments
═══════════════════════════════════════════════════════

Admin-only endpoints to manage which locations each staff member covers.
Routing rule (FAIL-OPEN): admins always receive everything; staff WITH
assignments receive only alerts from those locations; staff with NO
assignments receive everything — missing config must never hide a
safety incident.
"""
import hashlib
import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    Device, DeviceCredential, Location, StaffLocation, User,
)
from models.schemas import DeviceCreate, DeviceUpdate, StaffCreate, StaffLocationsUpdate
from api.dependencies import get_db, require_admin
from utils.auth import hash_password

router = APIRouter(prefix="/api/admin", tags=["Admin — FR16 routing"])
logger = logging.getLogger(__name__)

_VALID_DEVICE_STATUS = {"online", "offline", "error"}


def _device_out(device: Device) -> dict:
    return {
        "device_id":   device.device_id,
        "location_id": device.location_id,
        "status":      device.status,
    }


@router.get("/staff", summary="List users with their assigned location IDs")
async def list_staff(
    db:    Session = Depends(get_db),
    admin: User    = Depends(require_admin),
):
    users = db.query(User).all()
    assignments = db.query(StaffLocation).all()
    by_user: dict[str, list[str]] = {}
    for row in assignments:
        by_user.setdefault(row.user_id, []).append(row.location_id)

    return {
        "staff": [
            {
                "user_id":      u.user_id,
                "name":         u.name,
                "email":        u.email,
                "role":         u.role,
                "location_ids": by_user.get(u.user_id, []),
            }
            for u in users
        ]
    }


@router.post("/staff", status_code=201, summary="Create a staff/admin account")
async def create_staff(
    body:  StaffCreate,
    db:    Session = Depends(get_db),
    admin: User    = Depends(require_admin),
):
    # Normalize email so uniqueness and login (case-sensitive lookup) are stable.
    email = body.email.strip().lower()

    # Case-insensitive duplicate check (existing seeded emails are already lowercase).
    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing:
        raise HTTPException(409, "A user with that email already exists")

    # Validate every location id up front (all-or-nothing) — mirrors PUT .../locations.
    location_ids = list(dict.fromkeys(body.location_ids))   # de-dupe, keep order
    if location_ids:
        valid = {
            loc.location_id
            for loc in db.query(Location)
                         .filter(Location.location_id.in_(location_ids))
                         .all()
        }
        bad = [lid for lid in location_ids if lid not in valid]
        if bad:
            raise HTTPException(400, f"Unknown location_ids: {', '.join(bad)}")

    user = User(
        name            = body.name,
        email           = email,
        hashed_password = hash_password(body.password),   # never stored/returned in plaintext
        role            = body.role,
    )
    db.add(user)
    db.flush()   # assign user.user_id before creating StaffLocation rows (single commit)
    for lid in location_ids:
        db.add(StaffLocation(user_id=user.user_id, location_id=lid))
    db.commit()
    db.refresh(user)

    logger.info(f"Admin created account user_id={user.user_id} role={user.role} "
                f"locations={location_ids or 'ALL (unrestricted)'}")   # never log credentials
    return {
        "user_id":      user.user_id,
        "name":         user.name,
        "email":        user.email,
        "role":         user.role,
        "location_ids": location_ids,
        "message":      "Account created",
    }


@router.put("/staff/{user_id}/locations", summary="Replace a user's location assignments")
async def set_staff_locations(
    user_id: str,
    body:    StaffLocationsUpdate,
    db:      Session = Depends(get_db),
    admin:   User    = Depends(require_admin),
):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")

    # Validate every location id before touching the assignments (all-or-nothing)
    location_ids = list(dict.fromkeys(body.location_ids))   # de-dupe, keep order
    if location_ids:
        valid = {
            loc.location_id
            for loc in db.query(Location)
                         .filter(Location.location_id.in_(location_ids))
                         .all()
        }
        bad = [lid for lid in location_ids if lid not in valid]
        if bad:
            raise HTTPException(400, f"Unknown location_ids: {', '.join(bad)}")

    # Replace assignments atomically (single commit)
    db.query(StaffLocation).filter(StaffLocation.user_id == user_id).delete()
    for lid in location_ids:
        db.add(StaffLocation(user_id=user_id, location_id=lid))
    db.commit()

    logger.info(f"FR16: admin set locations for user {user_id} -> {location_ids or 'ALL (unrestricted)'}")
    return {
        "user_id":      user_id,
        "location_ids": location_ids,
        "message":      (
            "Assignments updated"
            if location_ids else
            "Assignments cleared — user now receives alerts from all locations (fail-open)"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# FR29 — device registry management  /  FR25 — per-device API keys
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/devices", summary="Register a device (FR29)")
async def register_device(
    body:  DeviceCreate,
    db:    Session = Depends(get_db),
    admin: User    = Depends(require_admin),
):
    # device_id charset/length is enforced by the DeviceCreate schema.
    location = db.query(Location).filter(Location.location_id == body.location_id).first()
    if not location:
        raise HTTPException(400, "Unknown location_id")

    status = body.status or "online"
    if status not in _VALID_DEVICE_STATUS:
        raise HTTPException(400, "Invalid status")

    if db.query(Device).filter(Device.device_id == body.device_id).first():
        raise HTTPException(409, "Device already exists")

    device = Device(device_id=body.device_id, location_id=body.location_id, status=status)
    db.add(device)
    db.commit()
    db.refresh(device)

    logger.info(f"FR29: admin registered device {device.device_id} at {device.location_id}")
    return _device_out(device)


@router.put("/devices/{device_id}", summary="Update a device (FR29)")
async def update_device(
    device_id: str,
    body:      DeviceUpdate,
    db:        Session = Depends(get_db),
    admin:     User    = Depends(require_admin),
):
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(404, "Device not found")

    if body.location_id is not None:
        location = db.query(Location).filter(Location.location_id == body.location_id).first()
        if not location:
            raise HTTPException(400, "Unknown location_id")
        device.location_id = body.location_id

    if body.status is not None:
        if body.status not in _VALID_DEVICE_STATUS:
            raise HTTPException(400, "Invalid status")
        device.status = body.status

    db.commit()
    db.refresh(device)

    logger.info(f"FR29: admin updated device {device.device_id}")
    return _device_out(device)


@router.post("/devices/{device_id}/key", summary="Issue/rotate a per-device API key (FR25)")
async def issue_device_key(
    device_id: str,
    db:        Session = Depends(get_db),
    admin:     User    = Depends(require_admin),
):
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(404, "Device not found")

    # Generate the key, persist only its SHA-256 digest. The plaintext is shown
    # once here and never stored or logged.
    api_key  = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    cred = db.query(DeviceCredential).filter(DeviceCredential.device_id == device_id).first()
    if cred:   # rotation — replace the existing hash
        cred.key_hash   = key_hash
        cred.created_at = datetime.utcnow()
    else:
        db.add(DeviceCredential(device_id=device_id, key_hash=key_hash))
    db.commit()

    logger.info(f"FR25: issued/rotated device key for {device_id}")   # never log key material
    return {
        "device_id": device_id,
        "api_key":   api_key,
        "message":   "Shown once — store it in the device's secrets.h",
    }


@router.delete("/devices/{device_id}/key", summary="Revoke a per-device API key (FR25)")
async def revoke_device_key(
    device_id: str,
    db:        Session = Depends(get_db),
    admin:     User    = Depends(require_admin),
):
    cred = db.query(DeviceCredential).filter(DeviceCredential.device_id == device_id).first()
    if not cred:
        raise HTTPException(404, "No device key to revoke")

    db.delete(cred)
    db.commit()

    logger.info(f"FR25: revoked device key for {device_id}")
    return {"device_id": device_id, "message": "Device key revoked"}
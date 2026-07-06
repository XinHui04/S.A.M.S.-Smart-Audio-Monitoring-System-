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
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models.database import Location, StaffLocation, User
from models.schemas import StaffLocationsUpdate
from api.dependencies import get_db, require_admin

router = APIRouter(prefix="/api/admin", tags=["Admin — FR16 routing"])
logger = logging.getLogger(__name__)


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
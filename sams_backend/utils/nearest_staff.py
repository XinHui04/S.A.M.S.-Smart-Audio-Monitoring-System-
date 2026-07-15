"""
utils/nearest_staff.py
FR30 — "the system shall identify the nearest or assigned staff based on
location zones."

Ranks staff by straight-line (Euclidean) distance between their location and
the alert's location on the dashboard's schematic map (LocationPosition.map_x
/ map_y, 0-100 percentages, see FR28). A staff member's location is either:

  1. A FRESH voluntary zone check-in (FR30 enhancement — StaffCheckin, see
     api/checkin.py), when one exists and is not older than the TTL. This
     REPLACES the assigned-zone distance entirely — a checked-in staff member
     is ranked from their live zone even if they have zero StaffLocation
     assignments.
  2. Otherwise, the minimum distance over their static StaffLocation
     assignments (unchanged legacy behavior).

DISPLAY-SIDE ONLY: this is a read-only hint surfaced on alert detail and the
live WebSocket payload so responders can see who is closest. It does NOT change
alert delivery — FR16 routing (who actually receives an alert) is unchanged and
still fail-open in api/alerts.py and services/websocket_manager.py.
"""
import math
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import LocationPosition, StaffCheckin, StaffLocation, User


def compute_nearest_staff(
    db: Session,
    location_id: str,
    limit: int = 3,
    ttl_seconds: Optional[int] = None,
) -> List[dict]:
    """
    Return up to ``limit`` staff nearest to the alert's location, sorted by
    (distance, name). See module docstring (FR30) — display-side only.

    ``ttl_seconds`` defaults to settings.checkin_ttl_seconds; pass explicitly
    in tests that need a deterministic freshness window.

    Rules:
      - If the alert location has no LocationPosition row, there are no
        coordinates to rank against → return [].
      - Only role == "staff" users are considered; admins are always excluded,
        even if they have a check-in or assignment.
      - A staff member with a FRESH check-in (checked_in_at within
        ttl_seconds, and the checked-in location has a LocationPosition) is
        ranked using that zone's distance instead of their assignments, and is
        rankable even with zero StaffLocation rows.
      - Otherwise, a staff member's distance is the minimum Euclidean distance
        over their assigned locations that have a LocationPosition. Assigned
        locations lacking a position are skipped; a staff member with no
        positioned assignment at all (and no fresh check-in) is excluded.
      - Staff whose ranked zone (check-in or assignment) is the alert's own
        location get distance 0.0.

    Each entry: {"user_id", "name", "distance": <float, 1dp>,
    "assigned_here": <bool>, "checked_in": <bool>}. "assigned_here" always
    reflects the alert location being among their StaffLocation assignments,
    regardless of which zone was used for ranking. No email or credential
    data is ever included.
    """
    origin = (
        db.query(LocationPosition)
          .filter(LocationPosition.location_id == location_id)
          .first()
    )
    if origin is None:
        return []

    if ttl_seconds is None:
        ttl_seconds = get_settings().checkin_ttl_seconds

    # Map every positioned location once: location_id -> (map_x, map_y).
    positions = {
        pos.location_id: (pos.map_x, pos.map_y)
        for pos in db.query(LocationPosition).all()
    }

    def distance_to(loc_id: str) -> Optional[float]:
        coord = positions.get(loc_id)
        if coord is None:
            return None
        return math.hypot(coord[0] - origin.map_x, coord[1] - origin.map_y)

    best: dict = {}

    # 1) Static assignments (legacy) — establishes assigned_here and the
    #    fallback distance for staff without a fresh check-in.
    rows = (
        db.query(User, StaffLocation)
          .join(StaffLocation, StaffLocation.user_id == User.user_id)
          .filter(User.role == "staff")
          .all()
    )
    for user, assignment in rows:
        assigned_here = assignment.location_id == location_id
        entry = best.get(user.user_id)
        if entry is None:
            entry = best[user.user_id] = {
                "user_id": user.user_id,
                "name": user.name,
                "distance": None,
                "assigned_here": False,
                "checked_in": False,
            }
        entry["assigned_here"] = entry["assigned_here"] or assigned_here

        distance = distance_to(assignment.location_id)
        if distance is not None and (entry["distance"] is None or distance < entry["distance"]):
            entry["distance"] = distance

    # 2) Fresh check-ins (FR30 enhancement) — overrides distance and makes an
    #    unassigned staff member rankable.
    freshness_cutoff = datetime.utcnow() - timedelta(seconds=ttl_seconds)
    checkin_rows = (
        db.query(User, StaffCheckin)
          .join(StaffCheckin, StaffCheckin.user_id == User.user_id)
          .filter(User.role == "staff")
          .filter(StaffCheckin.checked_in_at >= freshness_cutoff)
          .all()
    )
    for user, checkin in checkin_rows:
        distance = distance_to(checkin.location_id)
        if distance is None:
            continue   # checked-in zone has no map position — cannot rank on it

        entry = best.get(user.user_id)
        if entry is None:
            entry = best[user.user_id] = {
                "user_id": user.user_id,
                "name": user.name,
                "distance": None,
                "assigned_here": False,
                "checked_in": False,
            }
        entry["distance"] = distance
        entry["checked_in"] = True

    # Staff with no positioned assignment and no usable check-in are excluded.
    ranked = sorted(
        (e for e in best.values() if e["distance"] is not None),
        key=lambda e: (e["distance"], e["name"]),
    )
    return [
        {
            "user_id": e["user_id"],
            "name": e["name"],
            "distance": round(e["distance"], 1),
            "assigned_here": e["assigned_here"],
            "checked_in": e["checked_in"],
        }
        for e in ranked[:limit]
    ]

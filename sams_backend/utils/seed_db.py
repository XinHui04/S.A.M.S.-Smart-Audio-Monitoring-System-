"""
utils/seed_db.py
Seeds the dev SQLite database with:
  - School locations (toilet blocks, stairwells, etc.)
  - Edge devices linked to locations
  - Default admin user

Run with:
    python utils/seed_db.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models.database import (
    create_db_engine, get_session_factory, resolve_database_url,
    Location, Device, User, StaffLocation, LocationPosition,
)
from utils.auth import hash_password
from config.settings import get_settings

settings = get_settings()

LOCATIONS = [
    {"location_id": "loc-001", "location_name": "Toilet Block A (Ground Floor)"},
    {"location_id": "loc-002", "location_name": "Toilet Block B (First Floor)"},
    {"location_id": "loc-003", "location_name": "Stairwell North"},
    {"location_id": "loc-004", "location_name": "Stairwell South"},
    {"location_id": "loc-005", "location_name": "Secluded Corridor Block C"},
    {"location_id": "loc-006", "location_name": "Changing Room (Sports Hall)"},
]

# FR28: schematic school map coordinates (0-100 % of the map canvas) for each
# location — laid out as a plausible floor plan: toilets on the left/right
# wings, stairwells at the far corners, the corridor in the center, and the
# changing room toward the bottom (near the sports hall).
LOCATION_POSITIONS = [
    {"location_id": "loc-001", "map_x": 15, "map_y": 20},   # Toilet Block A — left wing, upper floor
    {"location_id": "loc-002", "map_x": 85, "map_y": 20},   # Toilet Block B — right wing, upper floor
    {"location_id": "loc-003", "map_x": 10, "map_y": 85},   # Stairwell North — bottom-left corner
    {"location_id": "loc-004", "map_x": 90, "map_y": 85},   # Stairwell South — bottom-right corner
    {"location_id": "loc-005", "map_x": 50, "map_y": 50},   # Secluded Corridor Block C — map center
    {"location_id": "loc-006", "map_x": 50, "map_y": 90},   # Changing Room (Sports Hall) — bottom center
]

DEVICES = [
    {"device_id": "esp32-001", "location_id": "loc-001", "status": "online"},
    {"device_id": "esp32-002", "location_id": "loc-002", "status": "online"},
    {"device_id": "esp32-003", "location_id": "loc-003", "status": "online"},
    {"device_id": "esp32-004", "location_id": "loc-004", "status": "online"},
    {"device_id": "esp32-005", "location_id": "loc-005", "status": "offline"},
    {"device_id": "esp32-006", "location_id": "loc-006", "status": "online"},
]

USERS = [
    {
        "name":     "Admin",
        "email":    "admin@school.edu.my",
        "password": "Admin@1234",
        "role":     "admin",
    },
    {
        "name":     "Cikgu Siti",
        "email":    "siti@school.edu.my",
        "password": "Staff@1234",
        "role":     "staff",
    },
]

# FR16: staff → location assignments (email → location_ids).
# Admin gets no rows — admins receive everything regardless.
STAFF_LOCATIONS = {
    "siti@school.edu.my": ["loc-001", "loc-003"],
}


def seed():
    engine  = create_db_engine(database_url=settings.database_url, sqlite_path=settings.sqlite_db_path)
    Session = get_session_factory(engine)
    db      = Session()

    print("Seeding locations...")
    for loc in LOCATIONS:
        if not db.query(Location).filter_by(location_id=loc["location_id"]).first():
            db.add(Location(**loc))
    db.commit()
    print(f"  {len(LOCATIONS)} locations ready.")

    print("Seeding location positions (FR28)...")
    for pos in LOCATION_POSITIONS:
        if not db.query(LocationPosition).filter_by(location_id=pos["location_id"]).first():
            db.add(LocationPosition(**pos))
    db.commit()
    print(f"  {len(LOCATION_POSITIONS)} location positions ready.")

    print("Seeding devices...")
    for dev in DEVICES:
        if not db.query(Device).filter_by(device_id=dev["device_id"]).first():
            db.add(Device(**dev))
    db.commit()
    print(f"  {len(DEVICES)} devices ready.")

    print("Seeding users...")
    for u in USERS:
        if not db.query(User).filter_by(email=u["email"]).first():
            db.add(User(
                name            = u["name"],
                email           = u["email"],
                hashed_password = hash_password(u["password"]),
                role            = u["role"],
            ))
    db.commit()
    print(f"  {len(USERS)} users ready.")

    print("Seeding staff location assignments (FR16)...")
    n_assignments = 0
    for email, location_ids in STAFF_LOCATIONS.items():
        user = db.query(User).filter_by(email=email).first()
        if not user:
            continue
        for lid in location_ids:
            if not db.query(StaffLocation).filter_by(user_id=user.user_id, location_id=lid).first():
                db.add(StaffLocation(user_id=user.user_id, location_id=lid))
            n_assignments += 1
    db.commit()
    print(f"  {n_assignments} staff-location assignments ready.")

    db.close()
    _url    = resolve_database_url(settings.database_url, settings.sqlite_db_path)
    _target = "Supabase Postgres" if _url.startswith("postgresql") else f"SQLite ({settings.sqlite_db_path})"
    print("\n[OK] Database seeded successfully!")
    print(f"   Target  : {_target}")
    print(f"   Admin   : admin@school.edu.my / Admin@1234")


if __name__ == "__main__":
    seed()

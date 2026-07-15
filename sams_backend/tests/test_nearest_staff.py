"""
tests/test_nearest_staff.py
FR30 — nearest / assigned staff ranking by map-coordinate distance.

Unit coverage of utils/nearest_staff.compute_nearest_staff plus an endpoint
check that GET /api/alerts/{alert_id} carries the read-only "nearest_staff"
list. This is display-side only — FR16 delivery routing is unchanged and is
covered by test_alert_authorization.py / test_routing.py.

Import-order note: this file sorts alphabetically before test_auth.py, so
pytest imports it first — by importing test_auth at the top (before any app
module) we install the deterministic test settings, exactly as the other
alerts tests do.
"""
from datetime import datetime, timedelta

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    STAFF_EMAIL, STAFF_PASSWORD,
)

from models.database import (  # noqa: E402
    User, Location, LocationPosition, StaffLocation, StaffCheckin, Device, Event, Alert,
)
from utils.nearest_staff import compute_nearest_staff  # noqa: E402


BASE_TS = datetime(2026, 6, 15, 10, 0, 0)


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


def seed_map(db_setup):
    """
    Three positioned locations forming a simple grid on the map, plus staff
    users assigned to them. Alert origin is loc-A at (0, 0).

        loc-A (0, 0)      — origin
        loc-B (30, 40)    — distance 50 from A
        loc-C (60, 80)    — distance 100 from A

    Users:
        alice  → loc-A (assigned_here, distance 0)
        bob    → loc-B (distance 50)
        carol  → loc-C (distance 100)
    """
    session = db_setup()
    try:
        session.add_all([
            Location(location_id="loc-A", location_name="Block A"),
            Location(location_id="loc-B", location_name="Block B"),
            Location(location_id="loc-C", location_name="Block C"),
            LocationPosition(location_id="loc-A", map_x=0.0,  map_y=0.0),
            LocationPosition(location_id="loc-B", map_x=30.0, map_y=40.0),
            LocationPosition(location_id="loc-C", map_x=60.0, map_y=80.0),
            User(user_id="u-alice", name="Alice", email="alice@sams-test.com",
                 hashed_password="x", role="staff"),
            User(user_id="u-bob", name="Bob", email="bob@sams-test.com",
                 hashed_password="x", role="staff"),
            User(user_id="u-carol", name="Carol", email="carol@sams-test.com",
                 hashed_password="x", role="staff"),
            StaffLocation(user_id="u-alice", location_id="loc-A"),
            StaffLocation(user_id="u-bob",   location_id="loc-B"),
            StaffLocation(user_id="u-carol", location_id="loc-C"),
        ])
        session.commit()
    finally:
        session.close()


# ── 1. Assigned-here staff ranks first with distance 0 ───────────────────────

def test_assigned_here_ranks_first_distance_zero(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        result = compute_nearest_staff(session, "loc-A")
    finally:
        session.close()

    assert result[0]["user_id"] == "u-alice"
    assert result[0]["distance"] == 0.0
    assert result[0]["assigned_here"] is True
    assert result[0]["checked_in"] is False
    # No email/credential leakage.
    assert set(result[0].keys()) == {
        "user_id", "name", "distance", "assigned_here", "checked_in",
    }


# ── 2. Ranking across locations by Euclidean distance ────────────────────────

def test_ranks_by_euclidean_distance(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        result = compute_nearest_staff(session, "loc-A")
    finally:
        session.close()

    assert [e["user_id"] for e in result] == ["u-alice", "u-bob", "u-carol"]
    assert [e["distance"] for e in result] == [0.0, 50.0, 100.0]
    assert [e["assigned_here"] for e in result] == [True, False, False]


# ── 3. Admins and unassigned staff excluded ──────────────────────────────────

def test_admins_and_unassigned_staff_excluded(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        # An admin assigned to loc-A must NOT appear.
        session.add(User(user_id="u-admin2", name="Admin Two",
                         email="admin2@sams-test.com", hashed_password="x",
                         role="admin"))
        session.add(StaffLocation(user_id="u-admin2", location_id="loc-A"))
        # A staff member with no StaffLocation row (fail-open recipient, but no
        # zone) must NOT be ranked.
        session.add(User(user_id="u-nomad", name="Nomad",
                         email="nomad@sams-test.com", hashed_password="x",
                         role="staff"))
        session.commit()

        result = compute_nearest_staff(session, "loc-A")
    finally:
        session.close()

    ids = {e["user_id"] for e in result}
    assert "u-admin2" not in ids
    assert "u-nomad" not in ids
    assert ids == {"u-alice", "u-bob", "u-carol"}


# ── 4. Missing position for the alert location → [] ──────────────────────────

def test_missing_origin_position_returns_empty(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        # loc-D exists and has staff, but has NO LocationPosition row.
        session.add(Location(location_id="loc-D", location_name="Block D"))
        session.add(User(user_id="u-dave", name="Dave",
                         email="dave@sams-test.com", hashed_password="x",
                         role="staff"))
        session.add(StaffLocation(user_id="u-dave", location_id="loc-D"))
        session.commit()

        assert compute_nearest_staff(session, "loc-D") == []
        # Also unknown / None location id → [].
        assert compute_nearest_staff(session, "no-such-loc") == []
        assert compute_nearest_staff(session, None) == []
    finally:
        session.close()


# ── 5. Staff whose assigned locations all lack positions are excluded ────────

def test_staff_with_no_positioned_assignment_excluded(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        # loc-X has no LocationPosition; Erin is only assigned there.
        session.add(Location(location_id="loc-X", location_name="Annex X"))
        session.add(User(user_id="u-erin", name="Erin",
                         email="erin@sams-test.com", hashed_password="x",
                         role="staff"))
        session.add(StaffLocation(user_id="u-erin", location_id="loc-X"))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10)
    finally:
        session.close()

    assert "u-erin" not in {e["user_id"] for e in result}


def test_unpositioned_assignment_skipped_but_user_kept(db_setup):
    """A user with one positioned and one unpositioned assignment is ranked on
    the positioned one only."""
    seed_map(db_setup)
    session = db_setup()
    try:
        session.add(Location(location_id="loc-X", location_name="Annex X"))
        # Bob also covers unpositioned loc-X — his distance stays 50 (from B).
        session.add(StaffLocation(user_id="u-bob", location_id="loc-X"))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10)
    finally:
        session.close()

    bob = next(e for e in result if e["user_id"] == "u-bob")
    assert bob["distance"] == 50.0


# ── 6. limit respected ───────────────────────────────────────────────────────

def test_limit_respected(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        assert len(compute_nearest_staff(session, "loc-A", limit=2)) == 2
        assert len(compute_nearest_staff(session, "loc-A", limit=1)) == 1
        # Default limit is 3.
        assert len(compute_nearest_staff(session, "loc-A")) == 3
    finally:
        session.close()


# ── 7. Alert detail endpoint includes nearest_staff ──────────────────────────

def test_alert_detail_includes_nearest_staff(client, db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        session.add_all([
            Device(device_id="dev-A", location_id="loc-A", status="online"),
            Event(event_id="evt-A", device_id="dev-A", timestamp=BASE_TS,
                  intensity=85.0, pitch=440.0, confidence_score=0.9),
            Alert(alert_id="alert-A", event_id="evt-A", severity="high",
                  status="active", created_at=BASE_TS),
        ])
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/alerts/alert-A", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert "nearest_staff" in body
    assert [e["user_id"] for e in body["nearest_staff"]] == [
        "u-alice", "u-bob", "u-carol",
    ]
    assert body["nearest_staff"][0]["assigned_here"] is True
    # Feed stays lean — no nearest_staff there.
    feed = client.get("/api/alerts/?status=all", headers=staff_headers(client))
    assert feed.status_code == 200
    assert "nearest_staff" not in feed.json()["alerts"][0]


# ── 8. FR30 check-in overrides assigned-zone distance ────────────────────────

def test_fresh_checkin_overrides_assigned_distance(db_setup):
    """Carol is statically assigned to loc-C (distance 100 from loc-A) but has
    voluntarily checked into loc-B — she should rank at distance 50, not 100,
    and "checked_in" should be true."""
    seed_map(db_setup)
    session = db_setup()
    try:
        session.add(StaffCheckin(
            user_id="u-carol", location_id="loc-B", checked_in_at=datetime.utcnow(),
        ))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10)
    finally:
        session.close()

    carol = next(e for e in result if e["user_id"] == "u-carol")
    assert carol["distance"] == 50.0
    assert carol["checked_in"] is True
    # assigned_here still reflects the STATIC assignment (loc-C != loc-A).
    assert carol["assigned_here"] is False


def test_expired_checkin_falls_back_to_assignment(db_setup):
    """A check-in older than the TTL is ignored — ranking falls back to the
    static assignment."""
    seed_map(db_setup)
    session = db_setup()
    try:
        stale = datetime.utcnow() - timedelta(seconds=999)
        session.add(StaffCheckin(
            user_id="u-carol", location_id="loc-B", checked_in_at=stale,
        ))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10, ttl_seconds=300)
    finally:
        session.close()

    carol = next(e for e in result if e["user_id"] == "u-carol")
    assert carol["distance"] == 100.0   # falls back to loc-C assignment
    assert carol["checked_in"] is False


def test_checkin_makes_unassigned_staff_rankable(db_setup):
    """A staff member with ZERO StaffLocation rows becomes rankable purely by
    a fresh check-in."""
    seed_map(db_setup)
    session = db_setup()
    try:
        session.add(User(user_id="u-nomad2", name="Nomad Two",
                         email="nomad2@sams-test.com", hashed_password="x",
                         role="staff"))
        session.add(StaffCheckin(
            user_id="u-nomad2", location_id="loc-B", checked_in_at=datetime.utcnow(),
        ))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10)
    finally:
        session.close()

    nomad = next(e for e in result if e["user_id"] == "u-nomad2")
    assert nomad["distance"] == 50.0
    assert nomad["checked_in"] is True
    assert nomad["assigned_here"] is False


def test_checkin_at_alert_zone_is_distance_zero(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        # Carol checks into the alert's own zone (loc-A).
        session.add(StaffCheckin(
            user_id="u-carol", location_id="loc-A", checked_in_at=datetime.utcnow(),
        ))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10)
    finally:
        session.close()

    carol = next(e for e in result if e["user_id"] == "u-carol")
    assert carol["distance"] == 0.0
    assert carol["checked_in"] is True


def test_admin_checkin_still_excluded(db_setup):
    seed_map(db_setup)
    session = db_setup()
    try:
        session.add(User(user_id="u-admin3", name="Admin Three",
                         email="admin3@sams-test.com", hashed_password="x",
                         role="admin"))
        session.add(StaffCheckin(
            user_id="u-admin3", location_id="loc-A", checked_in_at=datetime.utcnow(),
        ))
        session.commit()

        result = compute_nearest_staff(session, "loc-A", limit=10)
    finally:
        session.close()

    assert "u-admin3" not in {e["user_id"] for e in result}

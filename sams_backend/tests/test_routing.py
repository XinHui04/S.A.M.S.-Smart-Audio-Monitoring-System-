"""
tests/test_routing.py
Tests for FR16 — alert routing by role/location (FAIL-OPEN).

Covers:
  1. WebSocketManager._allowed unit tests (pure routing rule).
  2. broadcast_alert delivery filtering with fake websockets.
  3. Admin endpoints: GET /api/admin/staff and PUT /api/admin/staff/{id}/locations.
  4. Alert feed filtering (GET /api/alerts/) by staff location assignments.

Import-order note: this file sorts alphabetically AFTER test_auth.py, but we
still import test_auth at the top (before any app module) so the deterministic
test env vars / settings cache are guaranteed regardless of collection order —
same approach as test_alerts_flow.py.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    ADMIN_EMAIL, ADMIN_PASSWORD, STAFF_EMAIL, STAFF_PASSWORD,
)

from models.database import (  # noqa: E402
    Location, Device, Event, Alert, StaffLocation,
)
from services.websocket_manager import WebSocketManager  # noqa: E402


BASE_TS = datetime(2026, 6, 15, 10, 0, 0)


def admin_headers(client):
    return bearer(login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"])


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


# ── 1. WebSocketManager._allowed — pure routing rule ─────────────────────────

def test_allowed_admin_sees_all():
    admin = {"user_id": "u1", "role": "admin", "location_ids": ["loc-1"]}
    assert WebSocketManager._allowed(admin, "loc-1") is True
    assert WebSocketManager._allowed(admin, "loc-2") is True
    # Admin with no assignments still sees everything.
    admin_unassigned = {"user_id": "u1", "role": "admin", "location_ids": []}
    assert WebSocketManager._allowed(admin_unassigned, "loc-2") is True


def test_allowed_user_info_none_sees_all():
    # Dev mode / auth unconfigured → unrestricted.
    assert WebSocketManager._allowed(None, "loc-1") is True
    assert WebSocketManager._allowed(None, None) is True


def test_allowed_staff_without_assignments_sees_all_fail_open():
    empty = {"user_id": "u2", "role": "staff", "location_ids": []}
    none_ = {"user_id": "u2", "role": "staff", "location_ids": None}
    missing = {"user_id": "u2", "role": "staff"}
    assert WebSocketManager._allowed(empty, "loc-1") is True
    assert WebSocketManager._allowed(none_, "loc-1") is True
    assert WebSocketManager._allowed(missing, "loc-1") is True


def test_allowed_staff_with_assignments_is_restricted():
    staff = {"user_id": "u2", "role": "staff", "location_ids": ["loc-1"]}
    assert WebSocketManager._allowed(staff, "loc-1") is True
    assert WebSocketManager._allowed(staff, "loc-2") is False


def test_allowed_untagged_alert_goes_to_everyone():
    staff = {"user_id": "u2", "role": "staff", "location_ids": ["loc-1"]}
    assert WebSocketManager._allowed(staff, None) is True
    assert WebSocketManager._allowed(None, None) is True


# ── 2. broadcast_alert — delivery filtering with fake websockets ─────────────

def test_broadcast_alert_filters_by_assignment():
    manager = WebSocketManager()

    admin_ws = AsyncMock()
    staff_ws = AsyncMock()   # staff assigned to a DIFFERENT location
    # Register directly (connect() would try to accept a real handshake).
    manager.connections[admin_ws] = {
        "user_id": "user-admin", "role": "admin", "location_ids": [],
    }
    manager.connections[staff_ws] = {
        "user_id": "user-staff", "role": "staff", "location_ids": ["loc-other"],
    }

    asyncio.run(manager.broadcast_alert(
        alert_id="alert-1",
        event_id="evt-1",
        location_name="Toilet Block X",
        severity="high",
        threat_score=0.95,
        classification="scream",
        transcript="help",
        location_id="loc-X",
    ))

    admin_ws.send_text.assert_called_once()
    staff_ws.send_text.assert_not_called()

    # The payload the admin received carries the routing location_id.
    import json
    payload = json.loads(admin_ws.send_text.call_args.args[0])
    assert payload["type"] == "ALERT"
    assert payload["location_id"] == "loc-X"
    assert payload["alert_id"] == "alert-1"


def test_broadcast_alert_untagged_reaches_restricted_staff():
    manager = WebSocketManager()
    staff_ws = AsyncMock()
    manager.connections[staff_ws] = {
        "user_id": "user-staff", "role": "staff", "location_ids": ["loc-other"],
    }

    asyncio.run(manager.broadcast_alert(
        alert_id="alert-2",
        event_id="evt-2",
        location_name="Unknown",
        severity="low",
        threat_score=0.2,
        classification="noise",
        transcript="",
        location_id=None,   # untagged → everyone
    ))

    staff_ws.send_text.assert_called_once()


# ── 3. Admin endpoints — /api/admin/staff ────────────────────────────────────

def seed_locations(db_setup, location_ids=("loc-A", "loc-B")):
    session = db_setup()
    try:
        session.add_all([
            Location(location_id=lid, location_name=f"Block {lid[-1]}")
            for lid in location_ids
        ])
        session.commit()
    finally:
        session.close()


def test_admin_staff_list_forbidden_for_staff(client):
    resp = client.get("/api/admin/staff", headers=staff_headers(client))
    assert resp.status_code == 403


def test_admin_staff_list_returns_users_with_assignments(client, db_setup):
    seed_locations(db_setup)
    session = db_setup()
    try:
        session.add(StaffLocation(user_id="user-staff", location_id="loc-A"))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/admin/staff", headers=admin_headers(client))
    assert resp.status_code == 200
    staff = {u["user_id"]: u for u in resp.json()["staff"]}
    assert set(staff) == {"user-admin", "user-staff"}
    assert staff["user-staff"]["role"] == "staff"
    assert staff["user-staff"]["location_ids"] == ["loc-A"]
    assert staff["user-admin"]["location_ids"] == []


def test_put_locations_valid_assignment_replaces_rows(client, db_setup):
    seed_locations(db_setup)
    # Pre-existing assignment that must be REPLACED, not appended to.
    session = db_setup()
    try:
        session.add(StaffLocation(user_id="user-staff", location_id="loc-B"))
        session.commit()
    finally:
        session.close()

    resp = client.put(
        "/api/admin/staff/user-staff/locations",
        json={"location_ids": ["loc-A"]},
        headers=admin_headers(client),
    )
    assert resp.status_code == 200
    assert resp.json()["location_ids"] == ["loc-A"]

    session = db_setup()
    try:
        rows = session.query(StaffLocation).filter(
            StaffLocation.user_id == "user-staff").all()
        assert [(r.user_id, r.location_id) for r in rows] == [("user-staff", "loc-A")]
    finally:
        session.close()


def test_put_locations_forbidden_for_staff(client, db_setup):
    seed_locations(db_setup)
    resp = client.put(
        "/api/admin/staff/user-staff/locations",
        json={"location_ids": ["loc-A"]},
        headers=staff_headers(client),
    )
    assert resp.status_code == 403


def test_put_locations_unknown_location_400_and_no_change(client, db_setup):
    seed_locations(db_setup)
    session = db_setup()
    try:
        session.add(StaffLocation(user_id="user-staff", location_id="loc-A"))
        session.commit()
    finally:
        session.close()

    resp = client.put(
        "/api/admin/staff/user-staff/locations",
        json={"location_ids": ["loc-A", "loc-nope"]},
        headers=admin_headers(client),
    )
    assert resp.status_code == 400
    assert "loc-nope" in resp.json()["detail"]

    # All-or-nothing: existing assignment untouched.
    session = db_setup()
    try:
        rows = session.query(StaffLocation).filter(
            StaffLocation.user_id == "user-staff").all()
        assert [r.location_id for r in rows] == ["loc-A"]
    finally:
        session.close()


def test_put_locations_unknown_user_404(client, db_setup):
    seed_locations(db_setup)
    resp = client.put(
        "/api/admin/staff/no-such-user/locations",
        json={"location_ids": ["loc-A"]},
        headers=admin_headers(client),
    )
    assert resp.status_code == 404


# ── 4. Feed filtering — GET /api/alerts/ honours assignments ────────────────

def seed_two_location_alerts(db_setup):
    """loc-A and loc-B each get a device → event → alert chain."""
    session = db_setup()
    try:
        session.add_all([
            Location(location_id="loc-A", location_name="Block A"),
            Location(location_id="loc-B", location_name="Block B"),
            Device(device_id="dev-A", location_id="loc-A", status="online"),
            Device(device_id="dev-B", location_id="loc-B", status="online"),
            Event(event_id="evt-A", device_id="dev-A", timestamp=BASE_TS,
                  intensity=85.0, pitch=440.0, confidence_score=0.9),
            Event(event_id="evt-B", device_id="dev-B", timestamp=BASE_TS,
                  intensity=80.0, pitch=420.0, confidence_score=0.8),
            Alert(alert_id="alert-A", event_id="evt-A", severity="high",
                  status="active", created_at=BASE_TS),
            Alert(alert_id="alert-B", event_id="evt-B", severity="high",
                  status="active", created_at=BASE_TS),
        ])
        session.commit()
    finally:
        session.close()


def test_feed_filtering_by_assignment_and_fail_open_on_clear(client, db_setup):
    seed_two_location_alerts(db_setup)
    admin_hdrs = admin_headers(client)
    staff_hdrs = staff_headers(client)

    # Assign staff to loc-A only (via the admin API — end-to-end).
    resp = client.put(
        "/api/admin/staff/user-staff/locations",
        json={"location_ids": ["loc-A"]},
        headers=admin_hdrs,
    )
    assert resp.status_code == 200

    # Staff sees ONLY the loc-A alert.
    resp = client.get("/api/alerts/?status=all", headers=staff_hdrs)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [a["alert_id"] for a in body["alerts"]] == ["alert-A"]
    assert body["alerts"][0]["location_id"] == "loc-A"

    # Admin sees BOTH.
    resp = client.get("/api/alerts/?status=all", headers=admin_hdrs)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {a["alert_id"] for a in body["alerts"]} == {"alert-A", "alert-B"}

    # Clear the staff assignments → fail-open: staff sees both again.
    resp = client.put(
        "/api/admin/staff/user-staff/locations",
        json={"location_ids": []},
        headers=admin_hdrs,
    )
    assert resp.status_code == 200
    assert "fail-open" in resp.json()["message"]

    resp = client.get("/api/alerts/?status=all", headers=staff_hdrs)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {a["alert_id"] for a in body["alerts"]} == {"alert-A", "alert-B"}
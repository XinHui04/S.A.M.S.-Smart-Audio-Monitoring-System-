"""
tests/test_alert_authorization.py
Tests for FR16 — per-alert authorization on GET /{alert_id}, /acknowledge and
/resolve (staff with location assignments may only touch alerts from their
assigned locations; unassigned staff and admins are unrestricted — fail-open).

Also covers the pagination caps on GET /api/alerts/ (per_page 1–100).

Import-order note: this file sorts alphabetically before test_auth.py, so
pytest imports it first — by importing test_auth at the top (before any app
module), we guarantee the deterministic test settings are installed exactly
as test_auth.py documents (same approach as test_alerts_flow.py).
"""
from datetime import datetime

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    ADMIN_EMAIL, ADMIN_PASSWORD, STAFF_EMAIL, STAFF_PASSWORD,
)

from models.database import (  # noqa: E402
    Location, Device, Event, Alert, StaffLocation,
)


BASE_TS = datetime(2026, 6, 15, 10, 0, 0)


def admin_headers(client):
    return bearer(login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"])


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


def seed_two_location_alerts(db_setup, assign_staff_to=None):
    """loc-A and loc-B each get a device → event → alert chain.

    When `assign_staff_to` is given, user-staff is assigned to that location.
    """
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
        if assign_staff_to:
            session.add(StaffLocation(user_id="user-staff",
                                      location_id=assign_staff_to))
        session.commit()
    finally:
        session.close()


# ── 1. Assigned staff blocked from other locations (404, not 403) ────────────

def test_assigned_staff_gets_404_on_foreign_alert(client, db_setup):
    seed_two_location_alerts(db_setup, assign_staff_to="loc-A")
    headers = staff_headers(client)

    resp = client.get("/api/alerts/alert-B", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Alert not found"   # not enumerable


def test_assigned_staff_gets_404_on_foreign_acknowledge(client, db_setup):
    seed_two_location_alerts(db_setup, assign_staff_to="loc-A")

    resp = client.put("/api/alerts/alert-B/acknowledge",
                      headers=staff_headers(client))
    assert resp.status_code == 404

    # Untouched in the DB.
    session = db_setup()
    try:
        row = session.query(Alert).filter(Alert.alert_id == "alert-B").one()
        assert row.status == "active"
    finally:
        session.close()


def test_assigned_staff_gets_404_on_foreign_resolve(client, db_setup):
    seed_two_location_alerts(db_setup, assign_staff_to="loc-A")

    resp = client.put(
        "/api/alerts/alert-B/resolve",
        json={"resolution_notes": "should not work"},
        headers=staff_headers(client),
    )
    assert resp.status_code == 404

    session = db_setup()
    try:
        row = session.query(Alert).filter(Alert.alert_id == "alert-B").one()
        assert row.status == "active"
        assert row.resolution_notes is None
    finally:
        session.close()


# ── 2. Assigned staff CAN work alerts in their own location ─────────────────

def test_assigned_staff_full_lifecycle_own_location(client, db_setup):
    seed_two_location_alerts(db_setup, assign_staff_to="loc-A")
    headers = staff_headers(client)

    resp = client.get("/api/alerts/alert-A", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["alert_id"] == "alert-A"

    assert client.put("/api/alerts/alert-A/acknowledge",
                      headers=headers).status_code == 200

    resp = client.put(
        "/api/alerts/alert-A/resolve",
        json={"resolution_notes": "Handled on site."},
        headers=headers,
    )
    assert resp.status_code == 200

    session = db_setup()
    try:
        row = session.query(Alert).filter(Alert.alert_id == "alert-A").one()
        assert row.status == "resolved"
    finally:
        session.close()


# ── 3. Unassigned staff — fail-open, can access any alert ───────────────────

def test_unassigned_staff_can_access_any_alert(client, db_setup):
    seed_two_location_alerts(db_setup)   # no StaffLocation rows
    headers = staff_headers(client)

    assert client.get("/api/alerts/alert-A", headers=headers).status_code == 200
    assert client.get("/api/alerts/alert-B", headers=headers).status_code == 200
    assert client.put("/api/alerts/alert-B/acknowledge",
                      headers=headers).status_code == 200
    resp = client.put(
        "/api/alerts/alert-B/resolve",
        json={"resolution_notes": "handled by unassigned staff"},
        headers=headers,
    )
    assert resp.status_code == 200


# ── 4. Admin — unrestricted regardless of assignments ───────────────────────

def test_admin_can_access_any_alert(client, db_setup):
    seed_two_location_alerts(db_setup, assign_staff_to="loc-A")
    headers = admin_headers(client)

    assert client.get("/api/alerts/alert-A", headers=headers).status_code == 200
    assert client.get("/api/alerts/alert-B", headers=headers).status_code == 200
    assert client.put("/api/alerts/alert-B/acknowledge",
                      headers=headers).status_code == 200
    resp = client.put(
        "/api/alerts/alert-B/resolve",
        json={"resolution_notes": "admin handled"},
        headers=headers,
    )
    assert resp.status_code == 200


# ── 5. Pagination caps on the feed ───────────────────────────────────────────

def test_per_page_over_cap_rejected(client, db_setup):
    resp = client.get("/api/alerts/?per_page=101", headers=staff_headers(client))
    assert resp.status_code == 422


def test_per_page_zero_rejected(client, db_setup):
    resp = client.get("/api/alerts/?per_page=0", headers=staff_headers(client))
    assert resp.status_code == 422

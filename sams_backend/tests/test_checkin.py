"""
tests/test_checkin.py
FR30 — staff zone check-in API (api/checkin.py).

Import-order note: mirrors test_nearest_staff.py — import test_auth first so
the deterministic test settings (JWT secret, SQLite, etc.) are installed
before any other app module is imported.
"""
from datetime import datetime, timedelta

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    STAFF_EMAIL, STAFF_PASSWORD, ADMIN_EMAIL, ADMIN_PASSWORD,
)

from models.database import Location, StaffCheckin  # noqa: E402


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


def admin_headers(client):
    return bearer(login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"])


def seed_locations(db_setup):
    session = db_setup()
    try:
        session.add_all([
            Location(location_id="loc-A", location_name="Block A"),
            Location(location_id="loc-B", location_name="Block B"),
        ])
        session.commit()
    finally:
        session.close()


# ── GET ───────────────────────────────────────────────────────────────────────

def test_get_requires_auth(client):
    resp = client.get("/api/staff/checkin")
    assert resp.status_code == 401


def test_get_empty_returns_null_checkin_and_location_list(client, db_setup):
    seed_locations(db_setup)
    resp = client.get("/api/staff/checkin", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkin"] is None
    assert [loc["location_id"] for loc in body["locations"]] == ["loc-A", "loc-B"]
    assert body["ttl_seconds"] == 7200


def test_get_only_returns_callers_own_checkin(client, db_setup):
    """Admin's check-in must never leak through the staff caller's GET."""
    seed_locations(db_setup)
    admin_headers_ = admin_headers(client)
    put_resp = client.put("/api/staff/checkin", json={"location_id": "loc-A"},
                           headers=admin_headers_)
    assert put_resp.status_code == 200

    staff_resp = client.get("/api/staff/checkin", headers=staff_headers(client))
    assert staff_resp.status_code == 200
    assert staff_resp.json()["checkin"] is None


# ── PUT ───────────────────────────────────────────────────────────────────────

def test_put_requires_auth(client):
    resp = client.put("/api/staff/checkin", json={"location_id": "loc-A"})
    assert resp.status_code == 401


def test_put_valid_location(client, db_setup):
    seed_locations(db_setup)
    resp = client.put("/api/staff/checkin", json={"location_id": "loc-A"},
                       headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["location_id"] == "loc-A"
    assert body["location_name"] == "Block A"
    assert body["expired"] is False
    assert body["checked_in_at"]


def test_put_unknown_location_returns_400(client, db_setup):
    seed_locations(db_setup)
    resp = client.put("/api/staff/checkin", json={"location_id": "no-such-loc"},
                       headers=staff_headers(client))
    assert resp.status_code == 400


def test_put_twice_overwrites_no_duplicate_row(client, db_setup):
    seed_locations(db_setup)
    headers = staff_headers(client)
    client.put("/api/staff/checkin", json={"location_id": "loc-A"}, headers=headers)
    resp = client.put("/api/staff/checkin", json={"location_id": "loc-B"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["location_id"] == "loc-B"

    session = db_setup()
    try:
        rows = session.query(StaffCheckin).filter(
            StaffCheckin.user_id == "user-staff"
        ).all()
        assert len(rows) == 1
        assert rows[0].location_id == "loc-B"
    finally:
        session.close()


# ── DELETE ────────────────────────────────────────────────────────────────────

def test_delete_requires_auth(client):
    resp = client.delete("/api/staff/checkin")
    assert resp.status_code == 401


def test_delete_when_absent_returns_404(client, db_setup):
    seed_locations(db_setup)
    resp = client.delete("/api/staff/checkin", headers=staff_headers(client))
    assert resp.status_code == 404


def test_delete_after_checkin_removes_row(client, db_setup):
    seed_locations(db_setup)
    headers = staff_headers(client)
    client.put("/api/staff/checkin", json={"location_id": "loc-A"}, headers=headers)

    resp = client.delete("/api/staff/checkin", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "checked_out"

    get_resp = client.get("/api/staff/checkin", headers=headers)
    assert get_resp.json()["checkin"] is None


# ── Expiry flag ───────────────────────────────────────────────────────────────

def test_get_reports_expired_checkin(client, db_setup):
    seed_locations(db_setup)
    session = db_setup()
    try:
        stale = datetime.utcnow() - timedelta(seconds=99999)
        session.add(StaffCheckin(user_id="user-staff", location_id="loc-A",
                                  checked_in_at=stale))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/staff/checkin", headers=staff_headers(client))
    assert resp.status_code == 200
    checkin = resp.json()["checkin"]
    assert checkin is not None
    assert checkin["expired"] is True

"""
tests/test_admin_staff_create.py
Admin-only staff/admin account creation — POST /api/admin/staff.

Covers: authz (staff 403, unauthenticated 401), a valid create that can then
LOGIN + GET /api/auth/me, duplicate-email (exact + different case) 409,
password/role validation 422, unknown location_ids 400 with all-or-nothing
rollback, that assignments show up in GET /api/admin/staff, and that no
credential material is ever returned.

Import-order note: import test_auth first (before any app module) so the
deterministic test env vars / settings cache are installed — same approach as
test_devices.py / test_nearest_staff.py.
"""
import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    ADMIN_EMAIL, ADMIN_PASSWORD, STAFF_EMAIL, STAFF_PASSWORD,
)

from models.database import Location, User   # noqa: E402


def admin_headers(client):
    return bearer(login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"])


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


def seed_location(db_setup, location_id="loc-1", name="Toilet Block A"):
    session = db_setup()
    try:
        session.add(Location(location_id=location_id, location_name=name))
        session.commit()
    finally:
        session.close()


NEW = {"name": "New Teacher", "email": "new.teacher@sams-test.com",
       "password": "GoodPass1", "role": "staff"}


# ── Authorization ─────────────────────────────────────────────────────────────

def test_create_staff_forbidden_for_staff(client):
    resp = client.post("/api/admin/staff", json=NEW, headers=staff_headers(client))
    assert resp.status_code == 403


def test_create_staff_unauthenticated(client):
    resp = client.post("/api/admin/staff", json=NEW)
    assert resp.status_code == 401


# ── Happy path: create → login → me ──────────────────────────────────────────

def test_create_then_login_and_me(client):
    resp = client.post("/api/admin/staff", json=NEW, headers=admin_headers(client))
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "new.teacher@sams-test.com"
    assert body["role"] == "staff"
    assert body["location_ids"] == []
    assert body["user_id"]
    # No credential material leaks in the response.
    assert "hashed_password" not in body
    assert "password" not in body

    # The new account can log in with its plaintext credentials.
    login_resp = login(client, NEW["email"], NEW["password"])
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    me = client.get("/api/auth/me", headers=bearer(token))
    assert me.status_code == 200
    assert me.json()["email"] == "new.teacher@sams-test.com"
    assert me.json()["user_id"] == body["user_id"]


def test_email_normalized_to_lowercase_and_case_insensitive_login(client):
    payload = {**NEW, "email": "Mixed.Case@Sams-Test.com"}
    resp = client.post("/api/admin/staff", json=payload, headers=admin_headers(client))
    assert resp.status_code == 201
    assert resp.json()["email"] == "mixed.case@sams-test.com"

    # Login with a differently-cased email still resolves the same account.
    login_resp = login(client, "MIXED.CASE@sams-test.com", NEW["password"])
    assert login_resp.status_code == 200


# ── Duplicate email ──────────────────────────────────────────────────────────

def test_duplicate_email_conflict(client):
    r1 = client.post("/api/admin/staff", json=NEW, headers=admin_headers(client))
    assert r1.status_code == 201
    r2 = client.post("/api/admin/staff", json=NEW, headers=admin_headers(client))
    assert r2.status_code == 409


def test_duplicate_email_different_case_conflict(client):
    r1 = client.post("/api/admin/staff", json=NEW, headers=admin_headers(client))
    assert r1.status_code == 201
    r2 = client.post("/api/admin/staff",
                     json={**NEW, "email": "New.Teacher@SAMS-Test.com"},
                     headers=admin_headers(client))
    assert r2.status_code == 409


def test_duplicate_of_seeded_admin_email_conflict(client):
    resp = client.post("/api/admin/staff",
                       json={**NEW, "email": ADMIN_EMAIL.upper()},
                       headers=admin_headers(client))
    assert resp.status_code == 409


# ── Validation (422) ─────────────────────────────────────────────────────────

def test_password_too_short(client):
    resp = client.post("/api/admin/staff",
                       json={**NEW, "password": "Ab1"},
                       headers=admin_headers(client))
    assert resp.status_code == 422


def test_password_no_digit(client):
    resp = client.post("/api/admin/staff",
                       json={**NEW, "password": "OnlyLetters"},
                       headers=admin_headers(client))
    assert resp.status_code == 422
    # The submitted password must never be echoed back in the error.
    assert "OnlyLetters" not in resp.text


def test_password_no_letter(client):
    resp = client.post("/api/admin/staff",
                       json={**NEW, "password": "12345678"},
                       headers=admin_headers(client))
    assert resp.status_code == 422
    assert "12345678" not in resp.text


def test_bad_role_rejected(client):
    resp = client.post("/api/admin/staff",
                       json={**NEW, "role": "superuser"},
                       headers=admin_headers(client))
    assert resp.status_code == 422


# ── Location assignments ─────────────────────────────────────────────────────

def test_valid_location_ids_assigned(client, db_setup):
    seed_location(db_setup, "loc-1", "Toilet Block A")
    seed_location(db_setup, "loc-2", "Stairwell B")
    resp = client.post("/api/admin/staff",
                       json={**NEW, "location_ids": ["loc-1", "loc-2"]},
                       headers=admin_headers(client))
    assert resp.status_code == 201
    assert set(resp.json()["location_ids"]) == {"loc-1", "loc-2"}

    # They show up in GET /api/admin/staff.
    listing = client.get("/api/admin/staff", headers=admin_headers(client))
    assert listing.status_code == 200
    row = next(s for s in listing.json()["staff"] if s["email"] == NEW["email"])
    assert set(row["location_ids"]) == {"loc-1", "loc-2"}


def test_unknown_location_ids_rejected_and_no_user_created(client, db_setup):
    seed_location(db_setup, "loc-1", "Toilet Block A")
    resp = client.post("/api/admin/staff",
                       json={**NEW, "location_ids": ["loc-1", "loc-nope"]},
                       headers=admin_headers(client))
    assert resp.status_code == 400
    assert "loc-nope" in resp.json()["detail"]

    # All-or-nothing: the user row must NOT have been created.
    session = db_setup()
    try:
        assert session.query(User).filter(User.email == NEW["email"]).first() is None
    finally:
        session.close()
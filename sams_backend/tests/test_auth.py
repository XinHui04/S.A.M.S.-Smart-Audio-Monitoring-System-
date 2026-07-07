"""
tests/test_auth.py
Integration tests for FR23 — JWT auth (login, token guards, device API key).

Runs the REAL FastAPI app (main.app) via TestClient, with get_db overridden
to an in-memory SQLite DB (StaticPool → one shared connection) seeded with an
admin and a staff user.

Settings handling:
  config.settings.get_settings() is lru_cached and reads .env (which contains a
  real JWT_SECRET_KEY and a Supabase DATABASE_URL). BEFORE importing any app
  module we override the relevant env vars and clear the settings cache so the
  module-level singletons in api/dependencies.py are built from deterministic
  test values (local SQLite, MQTT off, local storage, known JWT secret).
  os.environ takes precedence over the .env file for pydantic-settings.

No network, no model downloads: NLPService lazy-loads, ScreamAnalyzer only
loads a TFLite model if the file exists, MQTT is disabled, storage is local.
"""
import os
import tempfile
from datetime import datetime, timedelta

import jwt as pyjwt
import pytest

# ── Deterministic settings BEFORE importing the app ──────────────────────────
TEST_SECRET = "test-secret-key-for-pytest-only"

_TMP_SQLITE = os.path.join(tempfile.gettempdir(), "sams_test_auth_singleton.db")

os.environ["JWT_SECRET_KEY"]  = TEST_SECRET
os.environ["DEVICE_API_KEY"]  = ""            # open mode by default; set per-test
os.environ["DATABASE_URL"]    = ""            # force SQLite — never touch Supabase
os.environ["SQLITE_DB_PATH"]  = _TMP_SQLITE   # throwaway file for the module singletons
os.environ["MQTT_ENABLED"]    = "false"
os.environ["STORAGE_BACKEND"] = "local"
# AudioCaptureService constructs a Supabase client at import time and rejects
# empty credentials; dummy values are fine — no request is ever sent (storage
# backend is "local" and no test touches Supabase).
os.environ["SUPABASE_URL"]    = "https://test-project.supabase.co"
# The client also insists the key parses as a JWT — any well-formed one will do.
os.environ["SUPABASE_SERVICE_KEY"] = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."      # {"alg":"HS256","typ":"JWT"}
    "eyJyb2xlIjoiYW5vbiJ9."                       # {"role":"anon"}
    "dGVzdC1zaWduYXR1cmUtbm90LXJlYWw"             # dummy signature
)
os.environ["GROQ_API_KEY"]    = ""

from config.settings import get_settings

get_settings.cache_clear()
_settings = get_settings()
assert _settings.jwt_secret_key == TEST_SECRET, (
    "Settings cache was already populated from .env before test env vars were set — "
    "test_auth.py must be imported before any app module."
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from main import app                       # noqa: E402  (must come after env setup)
from api.dependencies import get_db        # noqa: E402
import api.dependencies as deps            # noqa: E402
from models.database import (              # noqa: E402
    Base, User, Location, Device, Event, Alert, get_session_factory,
)
from utils.auth import hash_password       # noqa: E402

# api/dependencies.py captured cfg = get_settings() at import; confirm it is the
# very same cached object we configured (auth guards read deps.cfg directly).
assert deps.cfg is _settings

# NOTE: LoginRequest.email is EmailStr — email-validator rejects special-use
# domains like ".local", so the test users live on a plain .com domain.
ADMIN_EMAIL, ADMIN_PASSWORD = "admin@sams-test.com", "Admin@1234"
STAFF_EMAIL, STAFF_PASSWORD = "staff@sams-test.com", "Staff@1234"

# bcrypt is slow — hash once for the whole module.
_ADMIN_HASH = hash_password(ADMIN_PASSWORD)
_STAFF_HASH = hash_password(STAFF_PASSWORD)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_setup():
    """Fresh in-memory SQLite DB (shared connection via StaticPool) per test,
    seeded with one admin and one staff user. Overrides app's get_db."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionFactory = get_session_factory(engine)

    session = SessionFactory()
    admin = User(user_id="user-admin", name="Test Admin", email=ADMIN_EMAIL,
                 hashed_password=_ADMIN_HASH, role="admin")
    staff = User(user_id="user-staff", name="Test Staff", email=STAFF_EMAIL,
                 hashed_password=_STAFF_HASH, role="staff")
    session.add_all([admin, staff])
    session.commit()
    session.close()

    def override_get_db():
        db = SessionFactory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SessionFactory
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.fixture()
def client(db_setup):
    # No `with` block: we deliberately skip the lifespan (MQTT connect/disconnect).
    return TestClient(app)


def login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_token(sub, *, expired=False, secret=TEST_SECRET):
    now = datetime.utcnow()
    exp = now - timedelta(minutes=5) if expired else now + timedelta(minutes=30)
    return pyjwt.encode({"sub": sub, "iat": now - timedelta(minutes=10), "exp": exp},
                        secret, algorithm="HS256")


# ── 1. Login success ─────────────────────────────────────────────────────────

def test_login_success_admin(client):
    resp = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == ADMIN_EMAIL
    assert body["user"]["role"] == "admin"
    assert body["user"]["user_id"] == "user-admin"

    # Token is a valid JWT signed with the configured secret and carries sub/role.
    claims = pyjwt.decode(body["access_token"], TEST_SECRET, algorithms=["HS256"])
    assert claims["sub"] == "user-admin"
    assert claims["role"] == "admin"


# ── 2. Login failures — identical detail (no user enumeration) ───────────────

def test_login_wrong_password_and_unknown_email_same_detail(client):
    wrong_pw = login(client, ADMIN_EMAIL, "not-the-password")
    unknown  = login(client, "nobody@sams-test.com", "whatever123")

    assert wrong_pw.status_code == 401
    assert unknown.status_code == 401
    assert wrong_pw.json()["detail"] == "Invalid email or password"
    assert unknown.json()["detail"] == "Invalid email or password"
    assert wrong_pw.json()["detail"] == unknown.json()["detail"]


# ── 3. Protected endpoint requires bearer token ───────────────────────────────

def test_alerts_requires_token(client):
    resp = client.get("/api/alerts/")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_alerts_with_valid_token(client):
    token = login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"]
    resp = client.get("/api/alerts/", headers=bearer(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["alerts"] == []


# ── 4. Expired / garbage tokens ───────────────────────────────────────────────

def test_expired_token_rejected(client):
    token = make_token("user-admin", expired=True)
    resp = client.get("/api/alerts/", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token expired"


def test_garbage_token_rejected(client):
    resp = client.get("/api/alerts/", headers=bearer("this.is.garbage"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


def test_wrong_signature_token_rejected(client):
    token = make_token("user-admin", secret="some-other-secret")
    resp = client.get("/api/alerts/", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


# ── 5. /api/auth/me returns the staff profile ────────────────────────────────

def test_me_returns_staff_profile(client):
    token = login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"]
    resp = client.get("/api/auth/me", headers=bearer(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "user_id": "user-staff",
        "name":    "Test Staff",
        "email":   STAFF_EMAIL,
        "role":    "staff",
    }


# ── 6. Resolving an alert stamps the resolving user's id ─────────────────────

def test_resolve_alert_stamps_user_id(client, db_setup):
    session = db_setup()
    session.add_all([
        Location(location_id="loc-1", location_name="Toilet Block A"),
        Device(device_id="dev-1", location_id="loc-1", status="online"),
        Event(event_id="evt-1", device_id="dev-1",
              timestamp=datetime(2026, 6, 15, 10, 0, 0),
              intensity=85.0, pitch=440.0, confidence_score=0.9),
        Alert(alert_id="alert-1", event_id="evt-1", severity="high", status="active"),
    ])
    session.commit()
    session.close()

    token = login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"]
    resp = client.put(
        "/api/alerts/alert-1/resolve",
        json={"resolution_notes": "Checked the toilet block, false alarm."},
        headers=bearer(token),
    )
    assert resp.status_code == 200
    assert resp.json()["alert_id"] == "alert-1"

    session = db_setup()
    try:
        alert = session.query(Alert).filter(Alert.alert_id == "alert-1").one()
        assert alert.status == "resolved"
        assert alert.user_id == "user-staff"          # audit stamp
        assert alert.resolved_at is not None
        assert alert.resolution_notes == "Checked the toilet block, false alarm."
    finally:
        session.close()


# ── 7. Device API key gate on ingestion ──────────────────────────────────────

def test_device_key_gate(client, monkeypatch):
    # verify_device_key reads the module-level cfg (the cached Settings object);
    # monkeypatch restores the empty default afterwards (no cross-test pollution).
    monkeypatch.setattr(deps.cfg, "device_api_key", "testkey")

    # Missing key → 401
    resp = client.post("/api/events/audio", data={"device_id": "esp32-001"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid device API key"

    # Wrong key → 401
    resp = client.post("/api/events/audio", data={"device_id": "esp32-001"},
                       headers={"X-API-Key": "wrongkey"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid device API key"

    # Correct key but incomplete form → 422 proves the key gate passed.
    resp = client.post("/api/events/audio", data={"device_id": "esp32-001"},
                       headers={"X-API-Key": "testkey"})
    assert resp.status_code == 422


def test_device_key_open_mode_when_unset(client):
    # DEVICE_API_KEY is empty (default in this module) → gate is open, so an
    # incomplete form reaches validation and returns 422, not 401.
    assert deps.cfg.device_api_key == ""
    resp = client.post("/api/events/audio", data={"device_id": "esp32-001"})
    assert resp.status_code == 422


# ── 8. ?token= query fallback for the <audio> element ────────────────────────

def test_audio_stream_query_token_fallback(client):
    token = login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"]

    # Valid JWT via query param: auth passes, then the handler 404s on the
    # nonexistent event — proving get_current_user_query_ok accepted ?token=.
    resp = client.get(f"/api/events/no-such-event/audio?token={token}")
    assert resp.status_code == 404

    # No token at all → rejected before the handler runs.
    resp = client.get("/api/events/no-such-event/audio")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_audio_stream_query_token_invalid(client):
    resp = client.get("/api/events/no-such-event/audio?token=garbage.token.here")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"
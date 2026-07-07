"""
tests/test_rate_limit.py
IP-based rate limiting (slowapi) on POST /api/auth/login — 5/minute per IP.

The suite runs with the limiter disabled (tests/conftest.py sets
RATE_LIMIT_ENABLED=false before any app import). These tests flip
`limiter.enabled` back on per-test — slowapi checks the flag on every
request — and reset the in-memory counter storage between tests so limits
never leak into (or out of) other tests.

Settings/env bootstrapping mirrors tests/test_auth.py: deterministic env vars
are exported and the settings cache cleared BEFORE importing any app module.
"""
import os
import tempfile

import pytest

# ── Deterministic settings BEFORE importing the app (same as test_auth.py) ───
TEST_SECRET = "test-secret-key-for-pytest-only"
_TMP_SQLITE = os.path.join(tempfile.gettempdir(), "sams_test_auth_singleton.db")

os.environ["JWT_SECRET_KEY"]  = TEST_SECRET
os.environ["DEVICE_API_KEY"]  = ""
os.environ["DATABASE_URL"]    = ""
os.environ["SQLITE_DB_PATH"]  = _TMP_SQLITE
os.environ["MQTT_ENABLED"]    = "false"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["SUPABASE_URL"]    = "https://test-project.supabase.co"
os.environ["SUPABASE_SERVICE_KEY"] = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyb2xlIjoiYW5vbiJ9."
    "dGVzdC1zaWduYXR1cmUtbm90LXJlYWw"
)
os.environ["GROQ_API_KEY"]    = ""

from config.settings import get_settings

get_settings.cache_clear()
get_settings()

from fastapi.testclient import TestClient   # noqa: E402
from sqlalchemy import create_engine        # noqa: E402
from sqlalchemy.pool import StaticPool      # noqa: E402

from main import app                        # noqa: E402
from api.dependencies import get_db         # noqa: E402
from models.database import Base, User, get_session_factory   # noqa: E402
from utils.auth import hash_password        # noqa: E402
from utils.rate_limit import limiter        # noqa: E402

# NOTE: LoginRequest.email is EmailStr — email-validator rejects special-use
# domains like ".local", so the test user lives on a plain .com domain.
STAFF_EMAIL, STAFF_PASSWORD = "staff@sams-test.com", "Staff@1234"
_STAFF_HASH = hash_password(STAFF_PASSWORD)

LOGIN_LIMIT = 5   # must match @limiter.limit("5/minute") on /api/auth/login


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def rate_limit_on():
    """Enable the limiter for one test; reset counters and restore afterwards."""
    previous = limiter.enabled
    limiter.reset()          # clean slate — no counts from other tests
    limiter.enabled = True
    try:
        yield
    finally:
        limiter.enabled = previous
        limiter.reset()      # never leak counts into the rest of the suite


@pytest.fixture()
def client():
    """TestClient with a fresh in-memory SQLite DB seeded with one staff user."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionFactory = get_session_factory(engine)

    session = SessionFactory()
    session.add(User(user_id="user-staff", name="Test Staff", email=STAFF_EMAIL,
                     hashed_password=_STAFF_HASH, role="staff"))
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
        yield TestClient(app)   # no lifespan — skip MQTT connect/disconnect
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def login(client, password):
    return client.post("/api/auth/login",
                       json={"email": STAFF_EMAIL, "password": password})


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_login_returns_429_after_limit_exceeded(client, rate_limit_on):
    # First 5 attempts within the window are allowed (401 — wrong password,
    # proving the request reached the handler, not the limiter).
    for _ in range(LOGIN_LIMIT):
        resp = login(client, "wrong-password")
        assert resp.status_code == 401

    # 6th attempt from the same IP within the minute → throttled.
    resp = login(client, "wrong-password")
    assert resp.status_code == 429
    # Default slowapi handler: generic message, no internals leaked.
    body = resp.json()
    assert "5 per 1 minute" in body.get("error", "")

    # Correct credentials are throttled too — the limit is per-IP, pre-auth.
    resp = login(client, STAFF_PASSWORD)
    assert resp.status_code == 429


def test_login_not_throttled_when_limiter_disabled(client):
    # Suite default (conftest): limiter disabled → no 429 however many calls.
    assert limiter.enabled is False
    for _ in range(LOGIN_LIMIT + 3):
        resp = login(client, "wrong-password")
        assert resp.status_code == 401

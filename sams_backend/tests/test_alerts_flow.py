"""
tests/test_alerts_flow.py
Integration tests for FR17 (acknowledge alert) and FR20 (severity-ordered feed).

Reuses the TestClient + in-memory StaticPool SQLite setup from test_auth.py.

Import-order note: test_auth.py performs its env-var / settings-cache setup at
module import time and MUST run before any app module is imported. This file
sorts alphabetically before test_auth.py, so pytest imports it first — by
importing test_auth at the top (before any app module), we guarantee the
deterministic test settings are installed exactly as test_auth.py documents.
"""
from datetime import datetime, timedelta

import pytest

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    STAFF_EMAIL, STAFF_PASSWORD,
)

from models.database import Location, Device, Event, Alert  # noqa: E402


BASE_TS = datetime(2026, 6, 15, 10, 0, 0)


def seed_alerts(db_setup, alerts):
    """Seeds the Location → Device → Event chain plus the given Alert rows.

    `alerts` is a list of dicts with keys: alert_id, severity, status,
    and optional created_at.
    """
    session = db_setup()
    try:
        session.add_all([
            Location(location_id="loc-1", location_name="Toilet Block A"),
            Device(device_id="dev-1", location_id="loc-1", status="online"),
            Event(event_id="evt-1", device_id="dev-1",
                  timestamp=BASE_TS,
                  intensity=85.0, pitch=440.0, confidence_score=0.9),
        ])
        for spec in alerts:
            session.add(Alert(
                alert_id=spec["alert_id"],
                event_id="evt-1",
                severity=spec["severity"],
                status=spec.get("status", "active"),
                created_at=spec.get("created_at", BASE_TS),
            ))
        session.commit()
    finally:
        session.close()


def staff_headers(client):
    token = login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"]
    return bearer(token)


def get_alert_row(db_setup, alert_id):
    session = db_setup()
    try:
        return session.query(Alert).filter(Alert.alert_id == alert_id).one()
    finally:
        session.close()


# ── 1. FR17: acknowledge happy path ──────────────────────────────────────────

def test_acknowledge_happy_path(client, db_setup):
    seed_alerts(db_setup, [{"alert_id": "alert-1", "severity": "high", "status": "active"}])

    resp = client.put("/api/alerts/alert-1/acknowledge", headers=staff_headers(client))
    assert resp.status_code == 200
    assert resp.json() == {"message": "Alert acknowledged", "alert_id": "alert-1"}

    row = get_alert_row(db_setup, "alert-1")
    assert row.status == "acknowledged"
    assert row.user_id == "user-staff"          # audit stamp: who acknowledged it


# ── 2. Double acknowledge → 409 ──────────────────────────────────────────────

def test_double_acknowledge_conflict(client, db_setup):
    seed_alerts(db_setup, [{"alert_id": "alert-1", "severity": "high", "status": "active"}])
    headers = staff_headers(client)

    assert client.put("/api/alerts/alert-1/acknowledge", headers=headers).status_code == 200

    resp = client.put("/api/alerts/alert-1/acknowledge", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Already acknowledged"


# ── 3. Acknowledge a resolved alert → 409 ────────────────────────────────────

def test_acknowledge_resolved_conflict(client, db_setup):
    seed_alerts(db_setup, [{"alert_id": "alert-1", "severity": "high", "status": "resolved"}])

    resp = client.put("/api/alerts/alert-1/acknowledge", headers=staff_headers(client))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Already resolved"


# ── 4. Full lifecycle: acknowledge → resolve ─────────────────────────────────

def test_resolve_from_acknowledged(client, db_setup):
    seed_alerts(db_setup, [{"alert_id": "alert-1", "severity": "high", "status": "active"}])
    headers = staff_headers(client)

    assert client.put("/api/alerts/alert-1/acknowledge", headers=headers).status_code == 200

    resp = client.put(
        "/api/alerts/alert-1/resolve",
        json={"resolution_notes": "Handled on site."},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["alert_id"] == "alert-1"

    row = get_alert_row(db_setup, "alert-1")
    assert row.status == "resolved"
    assert row.resolved_at is not None
    assert row.resolution_notes == "Handled on site."
    assert row.user_id == "user-staff"


# ── 5. Acknowledge without token → 401 ───────────────────────────────────────

def test_acknowledge_requires_token(client, db_setup):
    seed_alerts(db_setup, [{"alert_id": "alert-1", "severity": "high", "status": "active"}])

    resp = client.put("/api/alerts/alert-1/acknowledge")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"

    # Untouched in the DB.
    assert get_alert_row(db_setup, "alert-1").status == "active"


# ── 6. FR20: severity-prioritized feed ordering ──────────────────────────────

def test_feed_orders_by_severity_then_recency(client, db_setup):
    # low is the NEWEST, high is in the middle, medium is the OLDEST —
    # severity rank must win over created_at.
    seed_alerts(db_setup, [
        {"alert_id": "alert-low",    "severity": "low",
         "created_at": BASE_TS + timedelta(hours=2)},   # newest
        {"alert_id": "alert-high",   "severity": "high",
         "created_at": BASE_TS + timedelta(hours=1)},   # middle
        {"alert_id": "alert-medium", "severity": "medium",
         "created_at": BASE_TS},                        # oldest
    ])

    resp = client.get("/api/alerts/?status=all", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [a["alert_id"] for a in body["alerts"]] == [
        "alert-high", "alert-medium", "alert-low",
    ]


def test_feed_newest_first_within_same_severity(client, db_setup):
    seed_alerts(db_setup, [
        {"alert_id": "alert-high-old", "severity": "high", "created_at": BASE_TS},
        {"alert_id": "alert-high-new", "severity": "high",
         "created_at": BASE_TS + timedelta(hours=1)},
    ])

    resp = client.get("/api/alerts/?status=all", headers=staff_headers(client))
    assert resp.status_code == 200
    assert [a["alert_id"] for a in resp.json()["alerts"]] == [
        "alert-high-new", "alert-high-old",
    ]


# ── 7. Status filters: open (default) and acknowledged ──────────────────────

@pytest.fixture()
def mixed_status_alerts(db_setup):
    seed_alerts(db_setup, [
        {"alert_id": "alert-active",       "severity": "high",   "status": "active"},
        {"alert_id": "alert-acknowledged", "severity": "medium", "status": "acknowledged"},
        {"alert_id": "alert-resolved",     "severity": "low",    "status": "resolved"},
    ])


def test_open_filter_returns_active_and_acknowledged(client, db_setup, mixed_status_alerts):
    headers = staff_headers(client)

    resp = client.get("/api/alerts/?status=open", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {a["alert_id"] for a in body["alerts"]} == {"alert-active", "alert-acknowledged"}

    # Default (no status param) is "open" — identical result set.
    default_resp = client.get("/api/alerts/", headers=headers)
    assert default_resp.status_code == 200
    assert ([a["alert_id"] for a in default_resp.json()["alerts"]]
            == [a["alert_id"] for a in body["alerts"]])


def test_acknowledged_filter_returns_only_acknowledged(client, db_setup, mixed_status_alerts):
    resp = client.get("/api/alerts/?status=acknowledged", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["alerts"][0]["alert_id"] == "alert-acknowledged"
    assert body["alerts"][0]["status"] == "acknowledged"


# ── 8. Stats include acknowledged_alerts ─────────────────────────────────────

def test_stats_counts_acknowledged(client, db_setup, mixed_status_alerts):
    resp = client.get("/api/alerts/stats", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_alerts"] == 1
    assert body["acknowledged_alerts"] == 1
    assert body["resolved_alerts"] == 1
    assert body["high"] == 1
    assert body["medium"] == 1
    assert body["low"] == 1

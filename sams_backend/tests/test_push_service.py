"""
tests/test_push_service.py
Tests for FR9/FR12 — Web Push notifications.

Two halves:
  1. The /api/push router (public-key, subscribe upsert, unsubscribe ownership).
  2. PushService.send_alert fan-out — FR16 routing (mirrors the WebSocket rule),
     minimal payload (no transcript / no threat score), and 404/410 pruning.

Import-order note: import test_auth at the top (before any app module) so the
deterministic test env vars / settings cache are installed — same approach as
test_routing.py / test_alerts_flow.py.
"""
import json

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    ADMIN_EMAIL, ADMIN_PASSWORD, STAFF_EMAIL, STAFF_PASSWORD,
)

from config.settings import get_settings              # noqa: E402
import services.push_service as push_mod               # noqa: E402
from services.push_service import PushService          # noqa: E402
from models.database import PushSubscription, StaffLocation, Location  # noqa: E402
from pywebpush import WebPushException                 # noqa: E402


VALID_ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def admin_headers(client):
    return bearer(login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"])


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


def sub_body(endpoint=VALID_ENDPOINT, p256dh="pub-key-material", auth="auth-secret"):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


# ── 1. GET /public-key ───────────────────────────────────────────────────────

def test_public_key_requires_auth(client):
    resp = client.get("/api/push/public-key")
    assert resp.status_code == 401


def test_public_key_returns_key_and_enabled_flag(client, monkeypatch):
    # Default test settings have empty VAPID keys → disabled.
    resp = client.get("/api/push/public-key", headers=staff_headers(client))
    assert resp.status_code == 200
    assert resp.json() == {"public_key": "", "enabled": False}

    # With keys configured, the public key is returned and enabled flips True.
    # (The endpoint reads the live cached Settings via get_settings().)
    live_settings = get_settings()
    monkeypatch.setattr(live_settings, "vapid_public_key", "PUBKEY123")
    monkeypatch.setattr(live_settings, "vapid_private_key", "PRIVKEY123")
    resp = client.get("/api/push/public-key", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"public_key": "PUBKEY123", "enabled": True}
    # The private key is never leaked.
    assert "PRIVKEY123" not in json.dumps(body)


# ── 2. POST /subscribe ───────────────────────────────────────────────────────

def test_subscribe_valid(client, db_setup):
    resp = client.post("/api/push/subscribe", json=sub_body(),
                       headers=staff_headers(client))
    assert resp.status_code == 200
    assert resp.json() == {"status": "subscribed"}

    session = db_setup()
    try:
        rows = session.query(PushSubscription).all()
        assert len(rows) == 1
        assert rows[0].user_id == "user-staff"
        assert rows[0].endpoint == VALID_ENDPOINT
    finally:
        session.close()


def test_subscribe_rejects_non_https_endpoint(client, db_setup):
    resp = client.post("/api/push/subscribe",
                       json=sub_body(endpoint="http://evil.example/x"),
                       headers=staff_headers(client))
    assert resp.status_code == 422


def test_subscribe_rejects_overlong_endpoint(client, db_setup):
    resp = client.post("/api/push/subscribe",
                       json=sub_body(endpoint="https://x/" + "a" * 1100),
                       headers=staff_headers(client))
    assert resp.status_code == 422


def test_subscribe_requires_auth(client):
    resp = client.post("/api/push/subscribe", json=sub_body())
    assert resp.status_code == 401


def test_subscribe_upserts_duplicate_endpoint(client, db_setup):
    # Staff subscribes first.
    assert client.post("/api/push/subscribe", json=sub_body(p256dh="old", auth="old"),
                       headers=staff_headers(client)).status_code == 200
    # Same device (endpoint) re-subscribes as the admin, with new keys.
    resp = client.post("/api/push/subscribe", json=sub_body(p256dh="new", auth="new"),
                       headers=admin_headers(client))
    assert resp.status_code == 200

    session = db_setup()
    try:
        rows = session.query(PushSubscription).all()
        assert len(rows) == 1                    # upsert, not a second row
        assert rows[0].user_id == "user-admin"   # re-pointed to current user
        assert rows[0].p256dh == "new"
        assert rows[0].auth == "new"
    finally:
        session.close()


# ── 3. DELETE /subscribe ─────────────────────────────────────────────────────

def test_unsubscribe_own(client, db_setup):
    client.post("/api/push/subscribe", json=sub_body(), headers=staff_headers(client))
    resp = client.request("DELETE", "/api/push/subscribe",
                          json={"endpoint": VALID_ENDPOINT},
                          headers=staff_headers(client))
    assert resp.status_code == 200
    assert resp.json() == {"status": "unsubscribed"}

    session = db_setup()
    try:
        assert session.query(PushSubscription).count() == 0
    finally:
        session.close()


def test_unsubscribe_other_users_subscription_404(client, db_setup):
    # Admin owns the subscription; a non-admin, non-owner (staff) cannot delete it.
    client.post("/api/push/subscribe", json=sub_body(), headers=admin_headers(client))
    resp = client.request("DELETE", "/api/push/subscribe",
                          json={"endpoint": VALID_ENDPOINT},
                          headers=staff_headers(client))
    assert resp.status_code == 404

    session = db_setup()
    try:
        assert session.query(PushSubscription).count() == 1   # untouched
    finally:
        session.close()


def test_unsubscribe_missing_404(client, db_setup):
    resp = client.request("DELETE", "/api/push/subscribe",
                          json={"endpoint": "https://fcm.googleapis.com/none"},
                          headers=staff_headers(client))
    assert resp.status_code == 404


# ── 4. PushService.send_alert — FR16 routing + minimal payload ───────────────

def _seed_two_subscriptions(db_setup):
    """admin (unrestricted) + staff assigned to loc-other, each with a push sub."""
    session = db_setup()
    try:
        session.add_all([
            Location(location_id="loc-other", location_name="Block Other"),
            StaffLocation(user_id="user-staff", location_id="loc-other"),
            PushSubscription(subscription_id="sub-admin", user_id="user-admin",
                             endpoint="https://push/admin", p256dh="a", auth="a"),
            PushSubscription(subscription_id="sub-staff", user_id="user-staff",
                             endpoint="https://push/staff", p256dh="s", auth="s"),
        ])
        session.commit()
    finally:
        session.close()


def test_send_alert_fr16_filtering_and_minimal_payload(db_setup, monkeypatch):
    _seed_two_subscriptions(db_setup)

    calls = []

    def fake_webpush(*, subscription_info, data, **kwargs):
        calls.append((subscription_info["endpoint"], data))
        return "ok"

    monkeypatch.setattr(push_mod, "webpush", fake_webpush)

    svc = PushService("PUB", "PRIV", "mailto:test@x.com")
    attempted = svc.send_alert(
        db_setup,
        alert_id="alert-1", event_id="evt-1",
        location_id="loc-X",              # staff is assigned to loc-other → excluded
        location_name="Block X", severity="high", timestamp="2026-06-15T10:00:00",
    )

    # FR16: admin (unrestricted) gets it; staff assigned elsewhere does NOT.
    assert attempted == 1
    endpoints = [c[0] for c in calls]
    assert endpoints == ["https://push/admin"]

    # Minimal payload — no transcript, no threat score.
    payload = json.loads(calls[0][1])
    assert payload["type"] == "ALERT"
    assert payload["alert_id"] == "alert-1"
    assert payload["severity"] == "high"
    assert payload["location_name"] == "Block X"
    assert "transcript" not in payload
    assert "threat_score" not in payload


def test_send_alert_untagged_reaches_restricted_staff(db_setup, monkeypatch):
    _seed_two_subscriptions(db_setup)
    calls = []
    monkeypatch.setattr(push_mod, "webpush",
                        lambda *, subscription_info, data, **k: calls.append(subscription_info["endpoint"]))

    svc = PushService("PUB", "PRIV", "mailto:test@x.com")
    svc.send_alert(db_setup, alert_id="a", event_id="e",
                   location_id=None,          # untagged → everyone
                   location_name="?", severity="low", timestamp="t")

    assert set(calls) == {"https://push/admin", "https://push/staff"}


def test_send_alert_prunes_410(db_setup, monkeypatch):
    _seed_two_subscriptions(db_setup)

    def fake_webpush(*, subscription_info, data, **kwargs):
        # The admin subscription is gone (410); the untagged alert targets all.
        if subscription_info["endpoint"] == "https://push/admin":
            exc = WebPushException("gone")
            exc.response = type("R", (), {"status_code": 410})()
            raise exc
        return "ok"

    monkeypatch.setattr(push_mod, "webpush", fake_webpush)

    svc = PushService("PUB", "PRIV", "mailto:test@x.com")
    svc.send_alert(db_setup, alert_id="a", event_id="e",
                   location_id=None, location_name="?", severity="low", timestamp="t")

    session = db_setup()
    try:
        remaining = {r.subscription_id for r in session.query(PushSubscription).all()}
        assert remaining == {"sub-staff"}    # the 410'd admin sub was pruned
    finally:
        session.close()


def test_send_alert_disabled_is_noop(db_setup):
    # No VAPID keys → disabled → no query, no error, returns 0.
    svc = PushService("", "", "mailto:test@x.com")
    assert svc.enabled is False
    assert svc.send_alert(db_setup, alert_id="a", event_id="e", location_id=None,
                          location_name="?", severity="low", timestamp="t") == 0

"""
tests/test_devices.py
FR25 — per-device API keys · FR29 — device registry + heartbeat liveness.

Covers what test_auth.py / test_rate_limit.py do NOT already exercise:
  1. GET  /api/devices/            — JWT required, derived status
                                     (heartbeat online/offline vs stored
                                     fallback), map_x/map_y, has_credential,
                                     and that the key hash is never exposed.
  2. POST /api/devices/heartbeat   — unknown device 404, known device updates
                                     last_seen, invalid device_id charset,
                                     per-device auth (enrolled vs un-enrolled
                                     fail-open fallback).
  3. Admin device endpoints (api/admin.py) — staff 403, register/update
                                     validation, key issue (plaintext once +
                                     stored hash), revoke.
  4. Ingestion auth regression — an ENROLLED device (DeviceCredential row)
                                     presenting the wrong key on
                                     POST /api/events/audio → 401. This is
                                     the per-device path; test_auth.py's
                                     test_device_key_gate only covers the
                                     global DEVICE_API_KEY fallback.

Import-order note: import test_auth at the top (before any app module) so the
deterministic test env vars / settings cache are installed — same approach as
test_nearest_staff.py / test_push_service.py.
"""
import hashlib
from datetime import datetime, timedelta

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    ADMIN_EMAIL, ADMIN_PASSWORD, STAFF_EMAIL, STAFF_PASSWORD,
)

from config.settings import get_settings                                  # noqa: E402
from models.database import (                                              # noqa: E402
    Device, DeviceHeartbeat, DeviceCredential, Location, LocationPosition,
)

cfg = get_settings()


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


# ═══════════════════════════════════════════════════════════════════════════
# 1. GET /api/devices/ — staff view, derived status
# ═══════════════════════════════════════════════════════════════════════════

def test_list_devices_requires_auth(client):
    resp = client.get("/api/devices/")
    assert resp.status_code == 401


def test_fresh_heartbeat_is_online(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-online", location_id="loc-1", status="offline"))
        session.add(DeviceHeartbeat(device_id="dev-online", last_seen=datetime.utcnow()))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/devices/", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    dev = next(d for d in body["devices"] if d["device_id"] == "dev-online")
    assert dev["status"] == "online"
    assert dev["status_source"] == "heartbeat"
    # stored status is preserved separately from the derived one
    assert dev["stored_status"] == "offline"


def test_stale_heartbeat_is_offline(client, db_setup):
    seed_location(db_setup)
    threshold = cfg.device_offline_after_seconds
    stale = datetime.utcnow() - timedelta(seconds=threshold + 60)

    session = db_setup()
    try:
        session.add(Device(device_id="dev-stale", location_id="loc-1", status="online"))
        session.add(DeviceHeartbeat(device_id="dev-stale", last_seen=stale))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/devices/", headers=staff_headers(client))
    assert resp.status_code == 200
    dev = next(d for d in resp.json()["devices"] if d["device_id"] == "dev-stale")
    assert dev["status"] == "offline"
    assert dev["status_source"] == "heartbeat"


def test_no_heartbeat_falls_back_to_stored_status(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-never-pinged", location_id="loc-1", status="error"))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/devices/", headers=staff_headers(client))
    assert resp.status_code == 200
    dev = next(d for d in resp.json()["devices"] if d["device_id"] == "dev-never-pinged")
    assert dev["status"] == "error"
    assert dev["status_source"] == "stored"
    assert dev["last_seen"] is None


def test_map_position_present_when_seeded(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(LocationPosition(location_id="loc-1", map_x=12.5, map_y=64.0))
        session.add(Device(device_id="dev-mapped", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/devices/", headers=staff_headers(client))
    dev = next(d for d in resp.json()["devices"] if d["device_id"] == "dev-mapped")
    assert dev["map_x"] == 12.5
    assert dev["map_y"] == 64.0


def test_unpositioned_device_has_null_map_coords(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-unmapped", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/devices/", headers=staff_headers(client))
    dev = next(d for d in resp.json()["devices"] if d["device_id"] == "dev-unmapped")
    assert dev["map_x"] is None
    assert dev["map_y"] is None


def test_has_credential_flag_and_no_hash_leak(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-enrolled", location_id="loc-1", status="online"))
        session.add(Device(device_id="dev-bare", location_id="loc-1", status="online"))
        session.add(DeviceCredential(device_id="dev-enrolled",
                                     key_hash=hashlib.sha256(b"secret").hexdigest()))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/devices/", headers=staff_headers(client))
    assert resp.status_code == 200
    devices = resp.json()["devices"]
    enrolled = next(d for d in devices if d["device_id"] == "dev-enrolled")
    bare = next(d for d in devices if d["device_id"] == "dev-bare")

    assert enrolled["has_credential"] is True
    assert bare["has_credential"] is False

    # The key hash must never appear anywhere in the response.
    import json as _json
    raw = _json.dumps(devices)
    assert "key_hash" not in raw
    assert hashlib.sha256(b"secret").hexdigest() not in raw


# ═══════════════════════════════════════════════════════════════════════════
# 2. POST /api/devices/heartbeat
# ═══════════════════════════════════════════════════════════════════════════

def test_heartbeat_unknown_device_404(client, db_setup):
    resp = client.post("/api/devices/heartbeat", data={"device_id": "no-such-device"})
    assert resp.status_code == 404


def test_heartbeat_updates_last_seen(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-hb", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    resp = client.post("/api/devices/heartbeat", data={"device_id": "dev-hb"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "dev-hb"
    assert body["last_seen"]

    session = db_setup()
    try:
        hb = session.query(DeviceHeartbeat).filter(DeviceHeartbeat.device_id == "dev-hb").first()
        assert hb is not None
        assert (datetime.utcnow() - hb.last_seen).total_seconds() < 30
    finally:
        session.close()

    # A second ping updates the same row (upsert), not a duplicate.
    resp2 = client.post("/api/devices/heartbeat", data={"device_id": "dev-hb"})
    assert resp2.status_code == 200
    session = db_setup()
    try:
        assert session.query(DeviceHeartbeat).filter(
            DeviceHeartbeat.device_id == "dev-hb"
        ).count() == 1
    finally:
        session.close()


def test_heartbeat_invalid_device_id_charset(client, db_setup):
    resp = client.post("/api/devices/heartbeat", data={"device_id": "bad id!"})
    assert resp.status_code == 400

    resp = client.post("/api/devices/heartbeat", json={"device_id": "bad;id"})
    assert resp.status_code == 400


def test_heartbeat_missing_device_id(client, db_setup):
    resp = client.post("/api/devices/heartbeat", data={})
    assert resp.status_code == 400


def test_heartbeat_enrolled_device_requires_correct_key(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-secure", location_id="loc-1", status="online"))
        session.add(DeviceCredential(
            device_id="dev-secure",
            key_hash=hashlib.sha256(b"correct-key").hexdigest(),
        ))
        session.commit()
    finally:
        session.close()

    # No key → 401
    resp = client.post("/api/devices/heartbeat", data={"device_id": "dev-secure"})
    assert resp.status_code == 401

    # Wrong key → 401
    resp = client.post("/api/devices/heartbeat", data={"device_id": "dev-secure"},
                       headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401

    # Correct key → 200
    resp = client.post("/api/devices/heartbeat", data={"device_id": "dev-secure"},
                       headers={"X-API-Key": "correct-key"})
    assert resp.status_code == 200


def test_heartbeat_unenrolled_device_open_when_no_global_key(client, db_setup, monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps.cfg, "device_api_key", "")   # explicit: open/demo mode

    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-open", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    # No credential row, no global key configured → fail-open, no header needed.
    resp = client.post("/api/devices/heartbeat", data={"device_id": "dev-open"})
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 3. Admin device endpoints (api/admin.py)
# ═══════════════════════════════════════════════════════════════════════════

def test_staff_forbidden_on_all_admin_device_endpoints(client, db_setup):
    seed_location(db_setup)
    headers = staff_headers(client)

    resp = client.post("/api/admin/devices", json={"device_id": "d1", "location_id": "loc-1"},
                       headers=headers)
    assert resp.status_code == 403

    resp = client.put("/api/admin/devices/whatever", json={"status": "offline"}, headers=headers)
    assert resp.status_code == 403

    resp = client.post("/api/admin/devices/whatever/key", headers=headers)
    assert resp.status_code == 403

    resp = client.request("DELETE", "/api/admin/devices/whatever/key", headers=headers)
    assert resp.status_code == 403


def test_admin_register_device(client, db_setup):
    seed_location(db_setup)
    resp = client.post("/api/admin/devices",
                       json={"device_id": "dev-new", "location_id": "loc-1"},
                       headers=admin_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "dev-new"
    assert body["location_id"] == "loc-1"
    assert body["status"] == "online"   # default


def test_admin_register_device_duplicate_409(client, db_setup):
    seed_location(db_setup)
    headers = admin_headers(client)
    body = {"device_id": "dev-dup", "location_id": "loc-1"}
    assert client.post("/api/admin/devices", json=body, headers=headers).status_code == 200
    resp = client.post("/api/admin/devices", json=body, headers=headers)
    assert resp.status_code == 409


def test_admin_register_device_bad_location_400(client, db_setup):
    resp = client.post("/api/admin/devices",
                       json={"device_id": "dev-orphan", "location_id": "no-such-loc"},
                       headers=admin_headers(client))
    assert resp.status_code == 400


def test_admin_update_device_location_and_status(client, db_setup):
    seed_location(db_setup, "loc-1", "Block A")
    seed_location(db_setup, "loc-2", "Block B")
    session = db_setup()
    try:
        session.add(Device(device_id="dev-upd", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    resp = client.put("/api/admin/devices/dev-upd",
                      json={"location_id": "loc-2", "status": "offline"},
                      headers=admin_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["location_id"] == "loc-2"
    assert body["status"] == "offline"


def test_admin_update_device_bad_status_rejected(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-badstatus", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    resp = client.put("/api/admin/devices/dev-badstatus",
                      json={"status": "not-a-real-status"},
                      headers=admin_headers(client))
    assert resp.status_code in (400, 422)


def test_admin_update_device_not_found_404(client, db_setup):
    resp = client.put("/api/admin/devices/no-such-device",
                      json={"status": "offline"},
                      headers=admin_headers(client))
    assert resp.status_code == 404


def test_admin_issue_key_returns_plaintext_once_and_stores_hash(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-key", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    resp = client.post("/api/admin/devices/dev-key/key", headers=admin_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    plaintext = body["api_key"]
    assert plaintext
    assert body["device_id"] == "dev-key"

    session = db_setup()
    try:
        cred = session.query(DeviceCredential).filter(
            DeviceCredential.device_id == "dev-key"
        ).first()
        assert cred is not None
        assert cred.key_hash == hashlib.sha256(plaintext.encode()).hexdigest()
        assert cred.key_hash != plaintext   # never stored raw
    finally:
        session.close()


def test_admin_issue_key_rotation_replaces_hash(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-rotate", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    headers = admin_headers(client)
    first = client.post("/api/admin/devices/dev-rotate/key", headers=headers).json()["api_key"]
    second = client.post("/api/admin/devices/dev-rotate/key", headers=headers).json()["api_key"]
    assert first != second

    session = db_setup()
    try:
        assert session.query(DeviceCredential).filter(
            DeviceCredential.device_id == "dev-rotate"
        ).count() == 1   # rotated in place, not duplicated
    finally:
        session.close()


def test_admin_issue_key_unknown_device_404(client, db_setup):
    resp = client.post("/api/admin/devices/no-such-device/key", headers=admin_headers(client))
    assert resp.status_code == 404


def test_admin_revoke_key(client, db_setup):
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-revoke", location_id="loc-1", status="online"))
        session.commit()
    finally:
        session.close()

    headers = admin_headers(client)
    client.post("/api/admin/devices/dev-revoke/key", headers=headers)

    resp = client.request("DELETE", "/api/admin/devices/dev-revoke/key", headers=headers)
    assert resp.status_code == 200

    session = db_setup()
    try:
        assert session.query(DeviceCredential).filter(
            DeviceCredential.device_id == "dev-revoke"
        ).count() == 0
    finally:
        session.close()


def test_admin_revoke_key_absent_404(client, db_setup):
    resp = client.request("DELETE", "/api/admin/devices/no-such-key/key",
                          headers=admin_headers(client))
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 4. Ingestion auth regression — enrolled device, wrong key, /api/events/audio
# ═══════════════════════════════════════════════════════════════════════════

def test_ingestion_enrolled_device_wrong_key_401(client, db_setup):
    """test_auth.py's test_device_key_gate only exercises the GLOBAL
    DEVICE_API_KEY fallback path. This covers the per-device (FR25)
    DeviceCredential path on the real ingestion endpoint."""
    seed_location(db_setup)
    session = db_setup()
    try:
        session.add(Device(device_id="dev-ingest", location_id="loc-1", status="online"))
        session.add(DeviceCredential(
            device_id="dev-ingest",
            key_hash=hashlib.sha256(b"real-key").hexdigest(),
        ))
        session.commit()
    finally:
        session.close()

    # Wrong key → 401, rejected before form-shape validation.
    resp = client.post("/api/events/audio", data={"device_id": "dev-ingest"},
                       headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401

    # Missing key entirely → 401.
    resp = client.post("/api/events/audio", data={"device_id": "dev-ingest"})
    assert resp.status_code == 401

    # Correct key + incomplete form → 422 proves the key gate passed.
    resp = client.post("/api/events/audio", data={"device_id": "dev-ingest"},
                       headers={"X-API-Key": "real-key"})
    assert resp.status_code == 422

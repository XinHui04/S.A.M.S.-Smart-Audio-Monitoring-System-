"""
tests/test_reports.py
Integration tests for FR13 — persistent incident reports (api/reports.py).

Covers:
  1. POST /api/reports/generate — admin-only guard + period totals.
  2. First-report-wins linking: re-generate links 0 events but still
     summarizes the whole period.
  3. GET /api/reports/ — newest-first listing with event counts (staff OK).
  4. GET /api/reports/{id} — live summary over LINKED events; unknown → 404.
  5. GET /api/reports/{id}/export.csv — content-type, attachment filename,
     header row, CSV-injection neutralization, row count, 404, auth.
  6. avg_minutes_to_resolve arithmetic.

Import-order note: this file sorts alphabetically AFTER test_auth.py, but we
still import test_auth at the top (before any app module) so the deterministic
test env vars / settings cache are guaranteed regardless of collection order —
same approach as test_alerts_flow.py / test_routing.py.
"""
import csv
import io
import time
from datetime import datetime, timedelta

import test_auth  # noqa: F401  — env setup side-effects; MUST precede app imports
from test_auth import (  # noqa: F401  — db_setup/client are pytest fixtures
    db_setup, client, login, bearer,
    ADMIN_EMAIL, ADMIN_PASSWORD, STAFF_EMAIL, STAFF_PASSWORD,
)

from models.database import (  # noqa: E402
    Location, Device, Event, Alert, AudioClip, Transcript, Analysis,
)


INJECTED_TRANSCRIPT = '=HYPERLINK("http://evil.example","click me")'

CSV_HEADER = [
    "event_id", "timestamp", "location", "severity",
    "status", "threat_score", "classification", "transcript",
]


def admin_headers(client):
    return bearer(login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()["access_token"])


def staff_headers(client):
    return bearer(login(client, STAFF_EMAIL, STAFF_PASSWORD).json()["access_token"])


def seed_report_data(db_setup):
    """Two locations, three in-period events (last 30 days) and one stale
    event (40 days old) that must fall outside a days=30 reporting period.

    In-period alert mix: one per severity, one per status; the resolved one
    closes exactly 45 minutes after creation. evt-1 also carries a full
    AudioClip → Transcript → Analysis chain with a formula-injection
    transcript for the CSV export test.
    """
    now = datetime.utcnow()
    session = db_setup()
    try:
        session.add_all([
            Location(location_id="loc-1", location_name="Toilet Block A"),
            Location(location_id="loc-2", location_name="Toilet Block B"),
            Device(device_id="dev-1", location_id="loc-1", status="online"),
            Device(device_id="dev-2", location_id="loc-2", status="online"),

            # In period (days=30): two events at loc-1, one at loc-2.
            Event(event_id="evt-1", device_id="dev-1",
                  timestamp=now - timedelta(days=1),
                  intensity=85.0, pitch=440.0, confidence_score=0.9),
            Event(event_id="evt-2", device_id="dev-1",
                  timestamp=now - timedelta(days=2),
                  intensity=70.0, pitch=300.0, confidence_score=0.7),
            Event(event_id="evt-3", device_id="dev-2",
                  timestamp=now - timedelta(days=3),
                  intensity=60.0, pitch=200.0, confidence_score=0.5),
            # Out of period: 40 days old — must be excluded from days=30.
            Event(event_id="evt-old", device_id="dev-1",
                  timestamp=now - timedelta(days=40),
                  intensity=90.0, pitch=500.0, confidence_score=0.95),

            Alert(alert_id="alert-1", event_id="evt-1", severity="high",
                  status="active", created_at=now - timedelta(days=1)),
            Alert(alert_id="alert-2", event_id="evt-2", severity="medium",
                  status="acknowledged", created_at=now - timedelta(days=2)),
            Alert(alert_id="alert-3", event_id="evt-3", severity="low",
                  status="resolved",
                  created_at=now - timedelta(days=3),
                  resolved_at=now - timedelta(days=3) + timedelta(minutes=45)),
            Alert(alert_id="alert-old", event_id="evt-old", severity="high",
                  status="active", created_at=now - timedelta(days=40)),

            # Audio chain for evt-1, with a spreadsheet-formula transcript.
            AudioClip(clip_id="clip-1", event_id="evt-1",
                      file_path="/tmp/clip1.wav", duration=5.0),
            Transcript(transcript_id="tr-1", clip_id="clip-1",
                       text=INJECTED_TRANSCRIPT),
            Analysis(analysis_id="an-1", transcript_id="tr-1",
                     severity_level="high", classification="threat",
                     threat_score=0.95),
        ])
        session.commit()
    finally:
        session.close()


def generate(client, headers, days=30):
    return client.post(f"/api/reports/generate?days={days}", headers=headers)


def assert_period_totals(body):
    """The expected summary over the three in-period seeded events."""
    assert body["totals"]["events"] == 3
    assert body["totals"]["alerts"] == 3
    assert body["totals"]["by_severity"] == {"high": 1, "medium": 1, "low": 1}
    assert body["totals"]["by_status"] == {
        "active": 1, "acknowledged": 1, "resolved": 1,
    }
    hotspots = {h["location_name"]: h["count"] for h in body["hotspots"]}
    assert hotspots == {"Toilet Block A": 2, "Toilet Block B": 1}
    # loc-1 has more events, so it must lead the hotspot ranking.
    assert body["hotspots"][0] == {"location_name": "Toilet Block A", "count": 2}
    assert body["resolution"]["resolved"] == 1


# ── 1. Generate — auth guards + period totals ────────────────────────────────

def test_generate_requires_token(client, db_setup):
    resp = client.post("/api/reports/generate")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_generate_forbidden_for_staff(client, db_setup):
    resp = generate(client, staff_headers(client))
    assert resp.status_code == 403


def test_generate_admin_totals_exclude_out_of_period(client, db_setup):
    seed_report_data(db_setup)

    resp = generate(client, admin_headers(client), days=30)
    assert resp.status_code == 200
    body = resp.json()

    assert body["report_id"]
    assert body["generated_date"]
    assert body["period_days"] == 30
    # evt-old (40 days ago) is neither linked nor counted.
    assert body["linked_events"] == 3
    assert_period_totals(body)


# ── 2. First-report-wins linking ─────────────────────────────────────────────

def test_regenerate_links_nothing_but_summarizes_full_period(client, db_setup):
    seed_report_data(db_setup)
    headers = admin_headers(client)

    first = generate(client, headers, days=30)
    assert first.status_code == 200
    assert first.json()["linked_events"] == 3

    second = generate(client, headers, days=30)
    assert second.status_code == 200
    body = second.json()
    assert body["report_id"] != first.json()["report_id"]
    # All period events already belong to the first report...
    assert body["linked_events"] == 0
    # ...yet the summary still covers the entire period.
    assert_period_totals(body)


# ── 3. List — newest first, event counts, staff allowed ──────────────────────

def test_list_reports_newest_first_with_event_counts(client, db_setup):
    seed_report_data(db_setup)
    headers = admin_headers(client)

    report_1 = generate(client, headers).json()["report_id"]
    time.sleep(0.01)  # guarantee distinct generated_date timestamps
    report_2 = generate(client, headers).json()["report_id"]

    # Staff may list reports (get_current_user, not require_admin).
    resp = client.get("/api/reports/", headers=staff_headers(client))
    assert resp.status_code == 200
    reports = resp.json()["reports"]

    assert [r["report_id"] for r in reports] == [report_2, report_1]  # newest first
    by_id = {r["report_id"]: r for r in reports}
    assert by_id[report_1]["event_count"] == 3   # claimed all period events
    assert by_id[report_2]["event_count"] == 0   # first-report-wins left nothing
    for r in reports:
        assert r["generated_date"]


def test_list_reports_requires_token(client, db_setup):
    resp = client.get("/api/reports/")
    assert resp.status_code == 401


# ── 4. Get by id ─────────────────────────────────────────────────────────────

def test_get_report_summary_over_linked_events(client, db_setup):
    seed_report_data(db_setup)
    report_id = generate(client, admin_headers(client)).json()["report_id"]

    resp = client.get(f"/api/reports/{report_id}", headers=staff_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["report_id"] == report_id
    assert body["generated_date"]
    assert_period_totals(body)


def test_get_report_unknown_id_404(client, db_setup):
    resp = client.get("/api/reports/no-such-report", headers=admin_headers(client))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Report not found"


# ── 5. CSV export ────────────────────────────────────────────────────────────

def test_csv_export_headers_rows_and_injection_defense(client, db_setup):
    seed_report_data(db_setup)
    report_id = generate(client, admin_headers(client)).json()["report_id"]

    resp = client.get(f"/api/reports/{report_id}/export.csv",
                      headers=staff_headers(client))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert (resp.headers["content-disposition"]
            == f'attachment; filename="sams_report_{report_id}.csv"')

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == CSV_HEADER

    data = rows[1:]
    assert len(data) == 3                      # one row per linked event/alert
    # Ordered by Event.timestamp ascending: evt-3 (oldest) … evt-1 (newest).
    assert [r[0] for r in data] == ["evt-3", "evt-2", "evt-1"]

    by_event = {r[0]: dict(zip(CSV_HEADER, r)) for r in data}

    # Formula-injection defense: leading '=' neutralized with an apostrophe.
    assert by_event["evt-1"]["transcript"] == "'" + INJECTED_TRANSCRIPT
    assert INJECTED_TRANSCRIPT not in [r[7] for r in data]

    assert by_event["evt-1"]["location"] == "Toilet Block A"
    assert by_event["evt-1"]["severity"] == "high"
    assert by_event["evt-1"]["status"] == "active"
    assert by_event["evt-1"]["classification"] == "threat"
    assert by_event["evt-3"]["location"] == "Toilet Block B"
    assert by_event["evt-3"]["status"] == "resolved"


def test_csv_export_unknown_id_404(client, db_setup):
    resp = client.get("/api/reports/no-such-report/export.csv",
                      headers=admin_headers(client))
    assert resp.status_code == 404


def test_csv_export_requires_token(client, db_setup):
    resp = client.get("/api/reports/some-report/export.csv")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


# ── 6. Resolution arithmetic ─────────────────────────────────────────────────

def test_avg_minutes_to_resolve_is_45(client, db_setup):
    seed_report_data(db_setup)  # alert-3 resolves exactly 45 min after creation
    resp = generate(client, admin_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolution"]["resolved"] == 1
    assert body["resolution"]["avg_minutes_to_resolve"] == 45.0

    # The persisted report recomputes the same figure.
    report_id = body["report_id"]
    detail = client.get(f"/api/reports/{report_id}",
                        headers=admin_headers(client)).json()
    assert detail["resolution"]["avg_minutes_to_resolve"] == 45.0
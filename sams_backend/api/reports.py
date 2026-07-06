"""
api/reports.py
═══════════════════════════════════════════════════════
MODULE 3: Report Generation (FR13)
═══════════════════════════════════════════════════════
Persistent incident reports built on the existing Report entity:

  POST /api/reports/generate          (admin)  create a report + summary
  GET  /api/reports/                  (auth)   list reports, newest first
  GET  /api/reports/{report_id}       (auth)   summary for one report
  GET  /api/reports/{id}/export.csv   (auth)   CSV of the report's events

Linking semantics (documented decision): an event belongs to its FIRST
report — POST /generate only claims events whose report_id is still NULL,
so re-generating never steals events from older reports. The generation
summary, however, is computed over ALL events in the period (linked or
not) so the numbers are always a complete period snapshot.
"""
import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from models.database import (
    Report, Event, Alert, Device, Location, AudioClip, Transcript, Analysis, User,
)
from api.dependencies import get_db, get_current_user, require_admin

router = APIRouter(prefix="/api/reports", tags=["Module 3 — Reports (FR13)"])
logger = logging.getLogger(__name__)


# ─── Internal helpers ────────────────────────────────────────────────────────

def _summarize_events(db: Session, event_ids: list[str]) -> dict:
    """
    Aggregate totals / hotspots / resolution stats over a set of event IDs.
    Averages are computed in Python so the code works identically on
    SQLite (dev/tests) and Postgres (Supabase).
    """
    if not event_ids:
        return {
            "totals": {
                "events": 0,
                "alerts": 0,
                "by_severity": {"high": 0, "medium": 0, "low": 0},
                "by_status":   {"active": 0, "acknowledged": 0, "resolved": 0},
            },
            "hotspots": [],
            "resolution": {"resolved": 0, "avg_minutes_to_resolve": None},
        }

    by_severity = {"high": 0, "medium": 0, "low": 0}
    for severity, count in (
        db.query(Alert.severity, func.count(Alert.alert_id))
        .filter(Alert.event_id.in_(event_ids))
        .group_by(Alert.severity)
        .all()
    ):
        if severity in by_severity:
            by_severity[severity] = count

    by_status = {"active": 0, "acknowledged": 0, "resolved": 0}
    for status, count in (
        db.query(Alert.status, func.count(Alert.alert_id))
        .filter(Alert.event_id.in_(event_ids))
        .group_by(Alert.status)
        .all()
    ):
        if status in by_status:
            by_status[status] = count

    total_alerts = (
        db.query(func.count(Alert.alert_id))
        .filter(Alert.event_id.in_(event_ids))
        .scalar()
    ) or 0

    hotspot_rows = (
        db.query(
            Location.location_name,
            func.count(Event.event_id).label("count"),
        )
        .join(Device, Device.location_id == Location.location_id)
        .join(Event,  Event.device_id    == Device.device_id)
        .filter(Event.event_id.in_(event_ids))
        .group_by(Location.location_id, Location.location_name)
        .order_by(desc("count"))
        .limit(5)
        .all()
    )

    resolved_rows = (
        db.query(Alert.created_at, Alert.resolved_at)
        .filter(
            Alert.event_id.in_(event_ids),
            Alert.status == "resolved",
            Alert.resolved_at.isnot(None),
        )
        .all()
    )
    durations = [
        (r.resolved_at - r.created_at).total_seconds() / 60.0
        for r in resolved_rows
        if r.created_at is not None
    ]
    avg_minutes = round(sum(durations) / len(durations), 1) if durations else None

    return {
        "totals": {
            "events": len(event_ids),
            "alerts": total_alerts,
            "by_severity": by_severity,
            "by_status":   by_status,
        },
        "hotspots": [
            {"location_name": r.location_name, "count": r.count} for r in hotspot_rows
        ],
        "resolution": {
            "resolved": by_status["resolved"],
            "avg_minutes_to_resolve": avg_minutes,
        },
    }


def _get_report_or_404(db: Session, report_id: str) -> Report:
    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(404, "Report not found")
    return report


def _csv_safe(value) -> str:
    """
    CSV-injection defense: spreadsheet apps execute cells that start with
    = + - or @ as formulas. Prefix a leading apostrophe so the cell is
    always treated as text. (Quoting alone does NOT stop formula execution.)
    """
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/generate", summary="Generate an incident report (admin only)")
async def generate_report(
    days: int = Query(30, ge=1, le=365, description="Reporting period in days"),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(require_admin),
):
    """
    Creates a Report row and links all not-yet-reported events from the last
    `days` days to it (first-report-wins: events already linked to an older
    report keep their original report). The returned summary covers ALL
    events in the period so it is always a complete snapshot.
    """
    since = datetime.utcnow() - timedelta(days=days)

    report = Report()
    db.add(report)
    db.flush()   # assign report_id before linking

    # Link only events not yet claimed by an earlier report.
    linked = (
        db.query(Event)
        .filter(Event.timestamp >= since, Event.report_id.is_(None))
        .update({Event.report_id: report.report_id}, synchronize_session=False)
    )

    # Summary over ALL events in the period, regardless of linkage.
    period_event_ids = [
        r.event_id
        for r in db.query(Event.event_id).filter(Event.timestamp >= since).all()
    ]
    summary = _summarize_events(db, period_event_ids)
    db.commit()

    logger.info("Report %s generated: %d-day period, %d events linked",
                report.report_id, days, linked)
    return {
        "report_id":      report.report_id,
        "generated_date": report.generated_date,
        "period_days":    days,
        "linked_events":  linked,
        **summary,
    }


@router.get("/", summary="List generated reports (newest first)")
async def list_reports(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    rows = (
        db.query(
            Report.report_id,
            Report.generated_date,
            func.count(Event.event_id).label("event_count"),
        )
        .outerjoin(Event, Event.report_id == Report.report_id)
        .group_by(Report.report_id, Report.generated_date)
        .order_by(desc(Report.generated_date))
        .all()
    )
    return {"reports": [
        {
            "report_id":      r.report_id,
            "generated_date": r.generated_date,
            "event_count":    r.event_count,
        }
        for r in rows
    ]}


@router.get("/{report_id}", summary="Summary for one report")
async def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    """
    Recomputes the summary live over the events LINKED to this report
    (Report stores no period, so linkage — not time range — defines its
    scope). Alert stats may therefore change as alerts get resolved.
    """
    report = _get_report_or_404(db, report_id)
    event_ids = [
        r.event_id
        for r in db.query(Event.event_id).filter(Event.report_id == report_id).all()
    ]
    summary = _summarize_events(db, event_ids)
    return {
        "report_id":      report.report_id,
        "generated_date": report.generated_date,
        **summary,
    }


@router.get("/{report_id}/export.csv", summary="Export a report's events as CSV")
async def export_report_csv(
    report_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    """
    Streams the report's linked events as CSV. Every cell passes through
    a CSV-injection filter (leading = + - @ neutralized with an apostrophe)
    and is written via the csv module with full quoting.
    """
    report = _get_report_or_404(db, report_id)

    rows = (
        db.query(
            Event.event_id,
            Event.timestamp,
            Location.location_name,
            Alert.severity,
            Alert.status,
            Analysis.threat_score,
            Analysis.classification,
            Analysis.severity_level,
            Transcript.text,
        )
        .join(Device,      Device.device_id      == Event.device_id)
        .join(Location,    Location.location_id  == Device.location_id)
        .outerjoin(AudioClip,  AudioClip.event_id     == Event.event_id)
        .outerjoin(Transcript, Transcript.clip_id     == AudioClip.clip_id)
        .outerjoin(Analysis,   Analysis.transcript_id == Transcript.transcript_id)
        .outerjoin(Alert,      Alert.event_id         == Event.event_id)
        .filter(Event.report_id == report.report_id)
        .order_by(Event.timestamp)
        .all()
    )

    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
        writer.writerow([
            "event_id", "timestamp", "location", "severity",
            "status", "threat_score", "classification", "transcript",
        ])
        yield buffer.getvalue()
        for r in rows:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([
                _csv_safe(r.event_id),
                _csv_safe(r.timestamp.isoformat() if r.timestamp else ""),
                _csv_safe(r.location_name),
                _csv_safe(r.severity or r.severity_level),   # alert severity, else analysis
                _csv_safe(r.status),
                _csv_safe(r.threat_score),
                _csv_safe(r.classification),
                _csv_safe(r.text),
            ])
            yield buffer.getvalue()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sams_report_{report.report_id}.csv"',
        },
    )
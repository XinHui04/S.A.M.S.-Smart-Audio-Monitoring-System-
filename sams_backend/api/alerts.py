"""
api/alerts.py
═══════════════════════════════════════════════════════
MODULE 3 & 4: Reporting + Main Monitoring Dashboard
═══════════════════════════════════════════════════════
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case
from sqlalchemy.orm import Session

from models.database import Alert, Event, Device, Location, AudioClip, Transcript, Analysis, User, StaffLocation, EmotionAnalysis
from models.schemas import AlertResolveRequest
from api.dependencies import get_db, get_current_user
from utils.nearest_staff import compute_nearest_staff

router = APIRouter(prefix="/api/alerts", tags=["Module 3+4 — Alerts & Dashboard"])
logger = logging.getLogger(__name__)


def _assert_alert_access(alert: Alert, user: User, db: Session) -> None:
    """
    FR16 per-alert authorization: staff with location assignments may only
    access alerts from their assigned locations.

    Allows when:
      - user is None (dev mode, auth unconfigured) or user is an admin;
      - the user has NO StaffLocation rows (fail-open — missing config must
        never hide a safety incident, consistent with the feed behavior);
      - the alert's location cannot be resolved (missing event/device —
        fail-open, same rationale).

    Otherwise raises 404 (NOT 403) when the alert's location is outside the
    user's assignments, so alert IDs are not enumerable by unauthorized staff.
    """
    if user is None or user.role == "admin":
        return

    assigned = [
        row.location_id
        for row in db.query(StaffLocation)
                     .filter(StaffLocation.user_id == user.user_id)
                     .all()
    ]
    if not assigned:
        return   # unassigned staff are unrestricted (fail-open)

    event  = alert.event
    device = db.query(Device).filter(Device.device_id == event.device_id).first() if event else None
    if device is None or device.location_id is None:
        return   # location unresolvable → fail-open

    if device.location_id not in assigned:
        # 404, not 403 — do not reveal that the alert ID exists.
        raise HTTPException(404, "Alert not found")


def _as_utc_iso(value: datetime | None) -> str | None:
    """
    Serialise a stored timestamp as an unambiguous UTC instant.

    Timestamps are written with datetime.utcnow(), so they are UTC but carry no
    tzinfo. isoformat() on a naive value emits no offset, and JavaScript parses
    a suffix-less datetime as LOCAL time — which displayed a 22:07 UTC alert as
    22:07 in a UTC+8 browser, eight hours early. Stamping the offset makes the
    instant explicit so clients convert it correctly.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def _parse_ts(value: str, field: str) -> datetime:
    """
    Parse a caller-supplied ISO-8601 instant into naive UTC for comparison
    against the stored (naive UTC) columns.

    Accepts a full instant ("2026-09-08T16:00:00Z", what the dashboard sends
    after converting the user's local date) and a bare date ("2026-09-09",
    read as UTC midnight) for direct API use.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(
            422, f"{field} must be ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)"
        )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _enrich_alert(alert: Alert, db: Session) -> dict:
    """Joins location, transcript, and analysis data onto an alert row."""
    event    = alert.event
    device   = db.query(Device).filter(Device.device_id == event.device_id).first() if event else None
    location = db.query(Location).filter(
        Location.location_id == device.location_id
    ).first() if device else None
    clip       = event.audio_clip if event else None
    transcript = clip.transcript  if clip  else None
    analysis   = transcript.analysis if transcript else None

    # SER result: the pipeline writes at most one EmotionAnalysis row per
    # event, so .first() is deterministic; order_by is defensive only.
    emo = db.query(EmotionAnalysis).filter(
        EmotionAnalysis.event_id == alert.event_id
    ).order_by(EmotionAnalysis.emotion_id).first()

    return {
        "alert_id":       alert.alert_id,
        "event_id":       alert.event_id,
        "severity":       alert.severity,
        "status":         alert.status,
        "created_at":     _as_utc_iso(alert.created_at),
        "resolved_at":    _as_utc_iso(alert.resolved_at),
        "location_name":  location.location_name if location else "Unknown",
        "location_id":    device.location_id if device else None,
        "transcript":     transcript.text if transcript else None,
        "threat_score":   analysis.threat_score if analysis else None,
        "final_threat_score": analysis.final_threat_score if analysis else None,
        "classification": analysis.classification if analysis else None,
        "intensity":      event.intensity if event else None,
        "pitch":          event.pitch if event else None,
        "edge_confidence": event.confidence_score if event else None,
        "audio_url":      f"/api/events/{alert.event_id}/audio" if clip else None,
        "emotion":            emo.emotion if emo else None,
        "emotion_confidence": emo.confidence if emo else None,
    }


@router.get("/", summary="List alerts — dashboard main feed")
async def list_alerts(
    status:   str = "open",   # active | acknowledged | resolved | open (active+acknowledged) | all
    severity: str = None,
    # Date range, inclusive of date_from and exclusive of date_to. The dashboard
    # converts the user's local calendar dates to UTC instants before sending,
    # so a filter reads as the operator's day, not the stored UTC day.
    date_from: str = Query(None, description="ISO-8601 instant or YYYY-MM-DD; inclusive"),
    date_to:   str = Query(None, description="ISO-8601 instant or YYYY-MM-DD; exclusive"),
    page:     int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    # FR20: severity-prioritized feed — high first, then medium, then low;
    # newest first within each severity band.
    severity_rank = case(
        (Alert.severity == "high",   0),
        (Alert.severity == "medium", 1),
        (Alert.severity == "low",    2),
        else_=3,
    )
    query = db.query(Alert).order_by(severity_rank, Alert.created_at.desc())

    # FR16: staff with location assignments only see alerts from those
    # locations. Admins, dev-mode (user=None) and UNASSIGNED staff see all
    # (fail-open — missing config must never hide a safety incident).
    # Note: /stats is intentionally left unfiltered (aggregate counts only);
    # GET /{alert_id}, /acknowledge and /resolve enforce the same rule via
    # _assert_alert_access.
    if user is not None and user.role != "admin":
        assigned = [
            row.location_id
            for row in db.query(StaffLocation)
                         .filter(StaffLocation.user_id == user.user_id)
                         .all()
        ]
        if assigned:
            query = (
                query.join(Event, Alert.event_id == Event.event_id)
                     .join(Device, Event.device_id == Device.device_id)
                     .filter(Device.location_id.in_(assigned))
            )

    if status == "open":
        query = query.filter(Alert.status.in_(["active", "acknowledged"]))
    elif status != "all":
        query = query.filter(Alert.status == status)
    if severity:
        query = query.filter(Alert.severity == severity)

    # Applied to the same query object, so the FR16 location restriction above
    # still constrains the result — a date filter must never widen visibility.
    if date_from:
        query = query.filter(Alert.created_at >= _parse_ts(date_from, "date_from"))
    if date_to:
        query = query.filter(Alert.created_at < _parse_ts(date_to, "date_to"))

    total  = query.count()
    alerts = query.offset((page - 1) * per_page).limit(per_page).all()

    return {
        "total":       total,
        "page":        page,
        "total_pages": (total + per_page - 1) // per_page,
        "alerts":      [_enrich_alert(a, db) for a in alerts],
    }


@router.get("/stats", summary="Dashboard header stats")
async def stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {
        "active_alerts":       db.query(Alert).filter(Alert.status == "active").count(),
        "acknowledged_alerts": db.query(Alert).filter(Alert.status == "acknowledged").count(),
        "resolved_alerts":     db.query(Alert).filter(Alert.status == "resolved").count(),
        "high":   db.query(Alert).filter(Alert.severity == "high").count(),
        "medium": db.query(Alert).filter(Alert.severity == "medium").count(),
        "low":    db.query(Alert).filter(Alert.severity == "low").count(),
    }


@router.get("/{alert_id}", summary="Get full alert detail")
async def get_alert(alert_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    alert = db.query(Alert).filter(Alert.alert_id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    _assert_alert_access(alert, user, db)   # FR16
    detail = _enrich_alert(alert, db)
    # FR30: display-side nearest-staff hint (read-only; delivery routing
    # unchanged). Kept off the lean feed (GET /) — detail view only.
    detail["nearest_staff"] = compute_nearest_staff(db, detail["location_id"])
    return detail


@router.put("/{alert_id}/acknowledge", summary="Staff acknowledges an alert — being handled")
async def acknowledge_alert(
    alert_id: str,
    db:       Session = Depends(get_db),
    user:     User    = Depends(get_current_user),
):
    alert = db.query(Alert).filter(Alert.alert_id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    _assert_alert_access(alert, user, db)   # FR16
    if alert.status == "acknowledged":
        raise HTTPException(409, "Already acknowledged")
    if alert.status == "resolved":
        raise HTTPException(409, "Already resolved")

    alert.status = "acknowledged"
    if user is not None:   # None only when auth is unconfigured (dev mode)
        alert.user_id = user.user_id   # audit: who acknowledged it
    db.commit()
    return {"message": "Alert acknowledged", "alert_id": alert_id}


@router.put("/{alert_id}/resolve", summary="Staff resolves an alert")
async def resolve_alert(
    alert_id: str,
    body:     AlertResolveRequest,
    db:       Session = Depends(get_db),
    user:     User    = Depends(get_current_user),
):
    alert = db.query(Alert).filter(Alert.alert_id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    _assert_alert_access(alert, user, db)   # FR16
    if alert.status == "resolved":
        raise HTTPException(409, "Already resolved")

    alert.status           = "resolved"
    alert.resolved_at      = datetime.utcnow()
    alert.resolution_notes = body.resolution_notes
    if user is not None:   # None only when auth is unconfigured (dev mode)
        alert.user_id = user.user_id   # audit: who resolved it
    db.commit()
    return {"message": "Alert resolved", "alert_id": alert_id}

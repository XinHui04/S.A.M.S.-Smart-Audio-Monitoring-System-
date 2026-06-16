"""
api/alerts.py
═══════════════════════════════════════════════════════
MODULE 3 & 4: Reporting + Main Monitoring Dashboard
═══════════════════════════════════════════════════════
"""
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models.database import Alert, Event, Device, Location, AudioClip, Transcript, Analysis
from models.schemas import AlertResolveRequest
from api.dependencies import get_db

router = APIRouter(prefix="/api/alerts", tags=["Module 3+4 — Alerts & Dashboard"])
logger = logging.getLogger(__name__)


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

    return {
        "alert_id":       alert.alert_id,
        "event_id":       alert.event_id,
        "severity":       alert.severity,
        "status":         alert.status,
        "created_at":     alert.created_at.isoformat(),
        "resolved_at":    alert.resolved_at.isoformat() if alert.resolved_at else None,
        "location_name":  location.location_name if location else "Unknown",
        "location_id":    device.location_id if device else None,
        "transcript":     transcript.text if transcript else None,
        "threat_score":   analysis.threat_score if analysis else None,
        "classification": analysis.classification if analysis else None,
        "intensity":      event.intensity if event else None,
        "pitch":          event.pitch if event else None,
        "edge_confidence": event.confidence_score if event else None,
        "audio_url":      f"/api/events/{alert.event_id}/audio" if clip else None,
    }


@router.get("/", summary="List alerts — dashboard main feed")
async def list_alerts(
    status:   str = "active",   # active | resolved | all
    severity: str = None,
    page:     int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
):
    query = db.query(Alert).order_by(Alert.created_at.desc())
    if status != "all":
        query = query.filter(Alert.status == status)
    if severity:
        query = query.filter(Alert.severity == severity)

    total  = query.count()
    alerts = query.offset((page - 1) * per_page).limit(per_page).all()

    return {
        "total":       total,
        "page":        page,
        "total_pages": (total + per_page - 1) // per_page,
        "alerts":      [_enrich_alert(a, db) for a in alerts],
    }


@router.get("/stats", summary="Dashboard header stats")
async def stats(db: Session = Depends(get_db)):
    return {
        "active_alerts":   db.query(Alert).filter(Alert.status == "active").count(),
        "resolved_alerts": db.query(Alert).filter(Alert.status == "resolved").count(),
        "high":   db.query(Alert).filter(Alert.severity == "high").count(),
        "medium": db.query(Alert).filter(Alert.severity == "medium").count(),
        "low":    db.query(Alert).filter(Alert.severity == "low").count(),
    }


@router.get("/{alert_id}", summary="Get full alert detail")
async def get_alert(alert_id: str, db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.alert_id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    return _enrich_alert(alert, db)


@router.put("/{alert_id}/resolve", summary="Staff resolves an alert")
async def resolve_alert(
    alert_id: str,
    body:     AlertResolveRequest,
    db:       Session = Depends(get_db),
):
    alert = db.query(Alert).filter(Alert.alert_id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    if alert.status == "resolved":
        raise HTTPException(409, "Already resolved")

    alert.status           = "resolved"
    alert.resolved_at      = datetime.utcnow()
    alert.resolution_notes = body.resolution_notes
    db.commit()
    return {"message": "Alert resolved", "alert_id": alert_id}

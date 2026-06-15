"""
api/analytics.py
═══════════════════════════════════════════════════════
MODULE 3: Reporting & Analytics
═══════════════════════════════════════════════════════
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from datetime import datetime, timedelta

from models.database import Event, Alert, Device, Location
from api.dependencies import get_db

router = APIRouter(prefix="/api/analytics", tags=["Module 3 — Reporting & Analytics"])


@router.get("/hotspots", summary="Top high-risk locations")
async def hotspots(limit: int = Query(5, ge=1, le=20), db: Session = Depends(get_db)):
    rows = (
        db.query(
            Location.location_id,
            Location.location_name,
            func.count(Alert.alert_id).label("count"),
        )
        .join(Device, Device.location_id == Location.location_id)
        .join(Event,  Event.device_id    == Device.device_id)
        .join(Alert,  Alert.event_id     == Event.event_id)
        .group_by(Location.location_id)
        .order_by(desc("count"))
        .limit(limit)
        .all()
    )
    return {"hotspots": [
        {"location_id": r.location_id, "location_name": r.location_name, "count": r.count}
        for r in rows
    ]}


@router.get("/trends", summary="Daily incident counts (last N days)")
async def trends(days: int = Query(30, ge=7, le=90), db: Session = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=days)
    rows  = (
        db.query(
            func.date(Alert.created_at).label("date"),
            Alert.severity,
            func.count(Alert.alert_id).label("count"),
        )
        .filter(Alert.created_at >= since)
        .group_by(func.date(Alert.created_at), Alert.severity)
        .order_by("date")
        .all()
    )
    trend_map = {}
    for r in rows:
        d = str(r.date)
        if d not in trend_map:
            trend_map[d] = {"date": d, "high": 0, "medium": 0, "low": 0, "total": 0}
        trend_map[d][r.severity] = r.count
        trend_map[d]["total"]   += r.count
    return {"days": days, "trend": list(trend_map.values())}


@router.get("/severity-breakdown", summary="Severity distribution for pie chart")
async def severity_breakdown(db: Session = Depends(get_db)):
    rows = (
        db.query(Alert.severity, func.count(Alert.alert_id).label("count"))
        .group_by(Alert.severity)
        .all()
    )
    return {"breakdown": [{"severity": r.severity, "count": r.count} for r in rows]}

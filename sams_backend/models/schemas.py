"""
models/schemas.py
Pydantic v2 schemas — what the API accepts and returns.
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ─── Inbound: Edge Device → Cloud ────────────────────────────────────────────

class AudioEventPayload(BaseModel):
    """
    Sent by ESP32-C3 edge device when a sound anomaly is detected.
    Audio file is uploaded as multipart/form-data alongside this metadata.
    """
    device_id:        str   = Field(..., description="Unique ID of the edge device")
    location_id:      str   = Field(..., description="Physical location of device")
    timestamp:        str   = Field(..., description="ISO8601 UTC timestamp")
    intensity:        float = Field(..., ge=0, description="Sound intensity in dB")
    pitch:            float = Field(..., ge=0, description="Dominant pitch in Hz")
    confidence_score: float = Field(..., ge=0.0, le=1.0,
                                    description="Edge-level scream confidence 0–1")
    duration_seconds: float = Field(..., gt=0, description="Audio clip length")


# ─── Outbound: Analysis Result ───────────────────────────────────────────────

class TranscriptResult(BaseModel):
    transcript_id: str
    text:          str

class AnalysisResult(BaseModel):
    analysis_id:    str
    severity_level: str        # low | medium | high
    classification: str        # verbal_bullying | threat | distress | normal
    threat_score:   float      # 0.0–1.0

class ProcessingResponse(BaseModel):
    """Full response returned after cloud processing completes."""
    event_id:    str
    clip_id:     str
    transcript:  TranscriptResult
    analysis:    AnalysisResult
    alert_fired: bool
    message:     str


# ─── Alerts ──────────────────────────────────────────────────────────────────

class AlertOut(BaseModel):
    alert_id:    str
    event_id:    str
    severity:    str
    status:      str
    location_id: Optional[str]
    location_name: Optional[str]
    timestamp:   datetime
    transcript_text: Optional[str]
    threat_score:    Optional[float]
    created_at:  datetime

    class Config:
        from_attributes = True

class AlertResolveRequest(BaseModel):
    resolution_notes: str = Field(..., min_length=5)


# ─── Auth ────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name:     str
    email:    str
    password: str
    role:     str = "staff"

class UserOut(BaseModel):
    user_id: str
    name:    str
    email:   str
    role:    str

    class Config:
        from_attributes = True

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user:         UserOut


# ─── Dashboard / Analytics ───────────────────────────────────────────────────

class IncidentSummary(BaseModel):
    total_events:      int
    high_severity:     int
    medium_severity:   int
    low_severity:      int
    active_alerts:     int
    resolved_alerts:   int

class HotspotLocation(BaseModel):
    location_id:   str
    location_name: str
    incident_count: int

class ReportOut(BaseModel):
    report_id:      str
    generated_date: datetime
    summary:        IncidentSummary
    hotspots:       list[HotspotLocation]

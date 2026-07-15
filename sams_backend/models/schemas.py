"""
models/schemas.py
Pydantic v2 schemas — what the API accepts and returns.
"""
from pydantic import BaseModel, EmailStr, Field, field_validator
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
    stt_confidence: Optional[float] = None   # 0–1, Whisper transcription confidence (how sure the STT is about the text)

class AnalysisResult(BaseModel):
    analysis_id:    str
    severity_level: str        # low | medium | high
    classification: str        # verbal_bullying | threat | distress | normal
    threat_score:   float      # 0.0–1.0
    nlp_confidence: Optional[float] = None   # 0–1, raw XLM-RoBERTa toxicity probability (before keyword boost)

class ProcessingResponse(BaseModel):
    """Full response returned after cloud processing completes."""
    event_id:    str
    clip_id:     str
    transcript:  TranscriptResult
    analysis:    AnalysisResult
    scream_confidence:  Optional[float] = None  # 0–1, edge-device scream detection confidence
    emotion:            Optional[str]   = None  # SER detected emotion (angry/fearful/...)
    emotion_confidence: Optional[float] = None  # 0–1, SER confidence for that emotion
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

class LoginRequest(BaseModel):
    email:    EmailStr = Field(..., description="Registered staff/admin email")
    password: str = Field(..., min_length=1, description="Plaintext password (verified against bcrypt hash)")

class UserCreate(BaseModel):
    name:     str
    email:    EmailStr
    password: str
    role:     str = "staff"

class UserOut(BaseModel):
    user_id: str
    name:    str
    email:   EmailStr
    role:    str

    class Config:
        from_attributes = True

class StaffLocationsUpdate(BaseModel):
    """FR16: replaces a staff user's location assignments (empty = unrestricted)."""
    location_ids: list[str] = Field(..., description="Location IDs assigned to the user")


class StaffCreate(BaseModel):
    """Admin-only: create a new staff/admin account (with optional FR16 assignments)."""
    name:     str = Field(..., description="Display name")
    email:    EmailStr = Field(..., description="Login email (stored lowercase)")
    password: str = Field(..., min_length=8, max_length=128,
                          description="Plaintext password (bcrypt-hashed before storage)")
    role:     str = Field("staff", description="staff | admin")
    location_ids: list[str] = Field(default_factory=list,
                                    description="Optional initial FR16 location assignments")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= 100):
            raise ValueError("name must be 1-100 characters after trimming")
        return v

    @field_validator("password")
    @classmethod
    def _password_complexity(cls, v: str) -> str:
        # Never echo the password back in the error message.
        if not any(c.isalpha() for c in v):
            raise ValueError("password must contain at least one letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one digit")
        return v

    @field_validator("role")
    @classmethod
    def _role_allowed(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"staff", "admin"}:
            raise ValueError("role must be 'staff' or 'admin'")
        return v


# ─── Devices (FR25 / FR29) ───────────────────────────────────────────────────

# device_id is echoed into logs and used as DB/object-key material, so it is
# constrained to a safe charset (blocks log-injection / control chars) and a
# sane length. Mirrors api/devices.py's validate_device_id.
_DEVICE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"

class DeviceCreate(BaseModel):
    """FR29: admin registers a new device at a location."""
    device_id:   str = Field(..., min_length=1, max_length=64, pattern=_DEVICE_ID_PATTERN)
    location_id: str = Field(..., min_length=1, description="Existing location ID")
    status:      Optional[str] = Field(None, description="online | offline | error")

class DeviceUpdate(BaseModel):
    """FR29: admin updates a device's location and/or stored status."""
    location_id: Optional[str] = Field(None, description="Existing location ID")
    status:      Optional[str] = Field(None, description="online | offline | error")

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


# ─── Web Push (FR9 / FR12) ───────────────────────────────────────────────────

class PushKeys(BaseModel):
    """Browser-supplied encryption keys from the PushSubscription object."""
    p256dh: str = Field(..., min_length=1, max_length=256)
    auth:   str = Field(..., min_length=1, max_length=64)


class PushSubscribeRequest(BaseModel):
    """
    Body of POST /api/push/subscribe — the browser's PushSubscription.
    endpoint is validated to be an https:// URL of a sane length (it is the push
    service address the server will POST to; must never be attacker-controlled
    http/other-scheme).
    """
    endpoint: str = Field(..., min_length=1, max_length=1024)
    keys:     PushKeys

    @field_validator("endpoint")
    @classmethod
    def _must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("endpoint must be an https:// URL")
        return v


class PushUnsubscribeRequest(BaseModel):
    """Body of DELETE /api/push/subscribe."""
    endpoint: str = Field(..., min_length=1, max_length=1024)


# ─── Staff Zone Check-in (FR30) ──────────────────────────────────────────────

class CheckinRequest(BaseModel):
    """Body of PUT /api/staff/checkin — the zone the caller is checking into."""
    location_id: str = Field(..., min_length=1, max_length=64,
                              description="Existing location ID")

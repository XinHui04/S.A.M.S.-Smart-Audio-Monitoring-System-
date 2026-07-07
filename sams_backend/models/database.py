"""
models/database.py
SQLAlchemy models (local SQLite for dev/tests, Supabase Postgres for cloud via DATABASE_URL).
Matches the ERD from your FYP report exactly.
"""
from sqlalchemy import (
    create_engine, Column, String, Float, DateTime,
    Text, Boolean, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import uuid

Base = declarative_base()


def generate_id() -> str:
    return str(uuid.uuid4())


# ─── Tables (mirror your ERD) ────────────────────────────────────────────────

class Location(Base):
    __tablename__ = "locations"

    location_id   = Column(String, primary_key=True, default=generate_id)
    location_name = Column(String, nullable=False)   # e.g. "Toilet Block A", "Stairwell B"

    devices = relationship("Device", back_populates="location")


class Device(Base):
    __tablename__ = "devices"

    device_id   = Column(String, primary_key=True, default=generate_id)
    location_id = Column(String, ForeignKey("locations.location_id"), nullable=False, index=True)
    status      = Column(String, default="online")   # online | offline | error

    location = relationship("Location", back_populates="devices")
    events   = relationship("Event", back_populates="device")


class Report(Base):
    __tablename__ = "reports"

    report_id      = Column(String, primary_key=True, default=generate_id)
    generated_date = Column(DateTime, default=datetime.utcnow)

    events = relationship("Event", back_populates="report")


class Event(Base):
    __tablename__ = "events"

    event_id         = Column(String, primary_key=True, default=generate_id)
    device_id        = Column(String, ForeignKey("devices.device_id"), nullable=False, index=True)
    report_id        = Column(String, ForeignKey("reports.report_id"), nullable=True, index=True)
    timestamp        = Column(DateTime, default=datetime.utcnow, index=True)
    intensity        = Column(Float)     # dB level from edge device
    pitch            = Column(Float)     # Hz
    confidence_score = Column(Float)     # 0.0–1.0 from edge scream classifier

    device     = relationship("Device", back_populates="events")
    report     = relationship("Report", back_populates="events")
    audio_clip = relationship("AudioClip", back_populates="event", uselist=False)
    alerts     = relationship("Alert", back_populates="event")


class AudioClip(Base):
    __tablename__ = "audio_clips"

    clip_id  = Column(String, primary_key=True, default=generate_id)
    event_id = Column(String, ForeignKey("events.event_id"), nullable=False, index=True)
    file_path = Column(String, index=True)   # Firebase Storage URL or local path
    duration  = Column(Float)    # seconds

    event      = relationship("Event", back_populates="audio_clip")
    transcript = relationship("Transcript", back_populates="audio_clip", uselist=False)


class Transcript(Base):
    __tablename__ = "transcripts"

    transcript_id = Column(String, primary_key=True, default=generate_id)
    clip_id       = Column(String, ForeignKey("audio_clips.clip_id"), nullable=False, index=True)
    text          = Column(Text)   # Full transcribed speech text

    audio_clip = relationship("AudioClip", back_populates="transcript")
    analysis   = relationship("Analysis", back_populates="transcript", uselist=False)


class Analysis(Base):
    __tablename__ = "analyses"

    analysis_id    = Column(String, primary_key=True, default=generate_id)
    transcript_id  = Column(String, ForeignKey("transcripts.transcript_id"), nullable=False, index=True)
    severity_level = Column(String)   # low | medium | high
    classification = Column(String)   # verbal_bullying | threat | distress | normal
    threat_score   = Column(Float)    # 0.0–1.0 final NLP confidence

    transcript = relationship("Transcript", back_populates="analysis")


class EmotionAnalysis(Base):
    """
    Speech Emotion Recognition result (report §2.1.3) — one row per event
    whenever the SER model returns a result, including non-negative emotions
    (analytics value). NEW table only: existing tables are never altered
    (live Postgres).
    """
    __tablename__ = "emotion_analyses"

    emotion_id = Column(String, primary_key=True, default=generate_id)
    event_id   = Column(String, ForeignKey("events.event_id"), nullable=False, index=True)
    emotion    = Column(String)   # angry | happy | neutral | sad | fearful | ...
    confidence = Column(Float)    # 0.0–1.0 top-class score


class User(Base):
    __tablename__ = "users"

    user_id       = Column(String, primary_key=True, default=generate_id)
    name          = Column(String, nullable=False)
    email         = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role          = Column(String, default="staff")   # admin | staff

    alerts = relationship("Alert", back_populates="user")


class StaffLocation(Base):
    """
    FR16: alert routing — assigns a staff user to the locations they cover.
    A staff user with NO rows here is unrestricted (fail-open: missing config
    must never hide a safety incident). Admins ignore this table entirely.
    """
    __tablename__ = "staff_locations"

    user_id     = Column(String, ForeignKey("users.user_id"), primary_key=True)
    location_id = Column(String, ForeignKey("locations.location_id"), primary_key=True)


class Alert(Base):
    __tablename__ = "alerts"

    alert_id  = Column(String, primary_key=True, default=generate_id)
    event_id  = Column(String, ForeignKey("events.event_id"), nullable=False, index=True)
    user_id   = Column(String, ForeignKey("users.user_id"), nullable=True, index=True)
    severity  = Column(String)          # low | medium | high
    status    = Column(String, default="active", index=True)   # active | resolved
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_notes = Column(Text, nullable=True)

    event = relationship("Event", back_populates="alerts")
    user  = relationship("User", back_populates="alerts")


# ─── DB Engine setup ─────────────────────────────────────────────────────────

def resolve_database_url(database_url: str = "", sqlite_path: str = "./sams.db") -> str:
    """
    Returns the SQLAlchemy URL to use.
    If database_url is set (e.g. a Supabase Postgres connection string) it wins;
    otherwise we fall back to a local SQLite file for dev / offline / tests.
    """
    return database_url.strip() or f"sqlite:///{sqlite_path}"


def create_db_engine(database_url: str = "", sqlite_path: str = "./sams.db"):
    """
    Build the engine for either Postgres (Supabase) or local SQLite.

    The check_same_thread connect arg is SQLite-only and must NOT be passed to
    Postgres. pool_pre_ping keeps pooled cloud connections healthy.
    """
    url = resolve_database_url(database_url, sqlite_path)

    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(url, pool_pre_ping=True)

    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)

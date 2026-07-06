"""
tests/test_processing_pipeline.py
Unit tests for ProcessingPipeline.process_stored_audio().

All external components (STT, NLP, WebSocket, MQTT) are mocked with
unittest.mock — no network, no model downloads, no real Groq calls.
The database is an in-memory SQLite created via models.database helpers.

Run with:
    pytest tests/ -v
"""
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.database import (
    create_db_engine,
    get_session_factory,
    Location,
    Device,
    Event,
    AudioClip,
    Transcript,
    Analysis,
    Alert,
)
from services.processing_pipeline import ProcessingPipeline


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    """Fresh in-memory SQLite DB per test (schema created by create_db_engine)."""
    engine = create_db_engine(sqlite_path=":memory:")
    SessionLocal = get_session_factory(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded(db_session):
    """Insert Location, Device, Event, AudioClip rows and return them."""
    location = Location(location_id="loc-1", location_name="Toilet Block A")
    device = Device(device_id="dev-1", location_id="loc-1", status="online")
    event = Event(
        event_id=str(uuid.uuid4()),
        device_id="dev-1",
        timestamp=datetime(2026, 6, 15, 10, 0, 0),
        intensity=85.0,
        pitch=440.0,
        confidence_score=0.5,
    )
    clip = AudioClip(
        clip_id=str(uuid.uuid4()),
        event_id=event.event_id,
        file_path="clips/test.wav",
        duration=3.0,
    )
    db_session.add_all([location, device, event, clip])
    db_session.commit()
    return SimpleNamespace(db=db_session, location=location, event=event, clip=clip)


def make_pipeline(stt_result=None, stt_exc=None, nlp_result=None):
    """Build a ProcessingPipeline with fully mocked components."""
    stt = MagicMock()
    if stt_exc is not None:
        stt.transcribe = AsyncMock(side_effect=stt_exc)
    else:
        stt.transcribe = AsyncMock(return_value=stt_result)

    nlp = MagicMock()
    nlp.analyse = AsyncMock(return_value=nlp_result)

    ws = MagicMock()
    ws.broadcast_alert = AsyncMock()

    mqtt = MagicMock()

    pipeline = ProcessingPipeline(
        audio_capture=MagicMock(),   # not used by process_stored_audio
        stt=stt,
        nlp=nlp,
        websocket_mgr=ws,
        mqtt=mqtt,
        threshold=0.75,
    )
    return pipeline


def nlp_threat(score, severity, classification):
    return SimpleNamespace(
        threat_score=score,
        severity_level=severity,
        classification=classification,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_threatening_speech_no_scream_fires_alert(seeded):
    """High NLP score without a scream => alert fires, all rows persisted."""
    text = "I will beat you up after school"
    pipeline = make_pipeline(
        stt_result={"text": text, "language": "en"},
        nlp_result=nlp_threat(0.9, "high", "verbal_threat"),
    )

    result = await pipeline.process_stored_audio(
        db=seeded.db,
        audio_bytes=b"fake-wav-bytes",
        event=seeded.event,
        clip=seeded.clip,
        location_id="loc-1",
        scream_confidence=0.2,
        is_scream=False,
    )

    assert result["alert_fired"] is True
    assert result["severity"] == "high"
    assert result["classification"] == "verbal_threat"
    assert result["transcript"] == text
    assert result["alert_id"] is not None

    # DB rows exist and carry the right data
    transcript = seeded.db.query(Transcript).filter(
        Transcript.clip_id == seeded.clip.clip_id
    ).one()
    assert transcript.text == text

    analysis = seeded.db.query(Analysis).filter(
        Analysis.transcript_id == transcript.transcript_id
    ).one()
    assert analysis.threat_score == 0.9
    assert analysis.severity_level == "high"

    alert = seeded.db.query(Alert).filter(
        Alert.event_id == seeded.event.event_id
    ).one()
    assert alert.alert_id == result["alert_id"]
    assert alert.status == "active"
    assert alert.severity == "high"

    pipeline.ws.broadcast_alert.assert_awaited_once()
    pipeline.mqtt.publish_alert.assert_called_once()


@pytest.mark.asyncio
async def test_harmless_speech_no_scream_no_alert(seeded):
    """Low NLP score, no scream => no alert, but Transcript + Analysis saved."""
    pipeline = make_pipeline(
        stt_result={"text": "see you at lunch", "language": "en"},
        nlp_result=nlp_threat(0.1, "low", "normal"),
    )

    result = await pipeline.process_stored_audio(
        db=seeded.db,
        audio_bytes=b"fake-wav-bytes",
        event=seeded.event,
        clip=seeded.clip,
        location_id="loc-1",
        scream_confidence=0.1,
        is_scream=False,
    )

    assert result["alert_fired"] is False
    assert result["alert_id"] is None

    assert seeded.db.query(Alert).count() == 0
    assert seeded.db.query(Transcript).filter(
        Transcript.clip_id == seeded.clip.clip_id
    ).count() == 1
    assert seeded.db.query(Analysis).count() == 1

    pipeline.ws.broadcast_alert.assert_not_awaited()
    pipeline.mqtt.publish_alert.assert_not_called()


@pytest.mark.asyncio
async def test_scream_with_harmless_speech_fires_alert(seeded):
    """is_scream=True overrides a low NLP score (combined alert rule)."""
    pipeline = make_pipeline(
        stt_result={"text": "help", "language": "en"},
        nlp_result=nlp_threat(0.1, "low", "normal"),
    )

    result = await pipeline.process_stored_audio(
        db=seeded.db,
        audio_bytes=b"fake-wav-bytes",
        event=seeded.event,
        clip=seeded.clip,
        location_id="loc-1",
        scream_confidence=0.95,
        is_scream=True,
    )

    assert result["alert_fired"] is True
    # Returned score is max(scream_confidence, nlp_score)
    assert result["threat_score"] == 0.95
    assert result["alert_id"] is not None

    assert seeded.db.query(Alert).count() == 1
    pipeline.ws.broadcast_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_stt_failure_fallback_scream_alert_still_fires(seeded):
    """STT raising must not propagate; scream alert still fires with placeholder."""
    pipeline = make_pipeline(
        stt_exc=Exception("Groq API unreachable"),
        nlp_result=nlp_threat(0.9, "high", "verbal_threat"),  # must NOT be called
    )

    result = await pipeline.process_stored_audio(
        db=seeded.db,
        audio_bytes=b"fake-wav-bytes",
        event=seeded.event,
        clip=seeded.clip,
        location_id="loc-1",
        scream_confidence=0.8,
        is_scream=True,
    )

    assert result["alert_fired"] is True
    assert result["transcript"] == "[transcription unavailable]"

    transcript = seeded.db.query(Transcript).filter(
        Transcript.clip_id == seeded.clip.clip_id
    ).one()
    assert transcript.text == "[transcription unavailable]"

    analysis = seeded.db.query(Analysis).filter(
        Analysis.transcript_id == transcript.transcript_id
    ).one()
    assert analysis.threat_score == 0.0
    assert analysis.classification == "scream"

    # NLP must be skipped when there is no usable transcript
    pipeline.nlp.analyse.assert_not_awaited()
    assert seeded.db.query(Alert).count() == 1
    pipeline.ws.broadcast_alert.assert_awaited_once()
"""
tests/test_ser_pipeline.py
Unit tests for the SER (Speech Emotion Recognition, report §2.1.3)
integration in ProcessingPipeline.process_stored_audio().

SER is mocked with AsyncMock — the real wav2vec2 model is never loaded and
no network is touched. STT, NLP, WebSocket and MQTT are mocked exactly as
in tests/test_processing_pipeline.py; the DB is a fresh in-memory SQLite.

Documented boost rule (services/processing_pipeline.py, Part C):
  emotion in {angry, fearful} AND confidence >= ser_min_confidence
      → threat_score += ser_boost (capped at 1.0),
  applied BEFORE the Analysis row is written and BEFORE the alert-threshold
  comparison. An EmotionAnalysis row is persisted for EVERY SER result.
  The returned dict / WS payload carry final_score =
  max(scream_confidence, boosted threat_score), while Analysis.threat_score
  stores the boosted NLP score itself.

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
    EmotionAnalysis,
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


def make_ser_mock(result):
    """Mock SERService whose analyse() resolves to `result` (dict or None)."""
    ser = MagicMock()
    ser.analyse = AsyncMock(return_value=result)
    return ser


def ser_result(emotion, confidence):
    return {
        "emotion":    emotion,
        "confidence": confidence,
        "all_scores": {emotion: confidence},
    }


def make_pipeline(stt_result=None, nlp_result=None, ser=None):
    """Build a ProcessingPipeline with fully mocked components + optional SER."""
    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value=stt_result)

    nlp = MagicMock()
    nlp.analyse = AsyncMock(return_value=nlp_result)

    ws = MagicMock()
    ws.broadcast_alert = AsyncMock()

    mqtt = MagicMock()

    return ProcessingPipeline(
        audio_capture=MagicMock(),   # not used by process_stored_audio
        stt=stt,
        nlp=nlp,
        websocket_mgr=ws,
        mqtt=mqtt,
        threshold=0.75,
        ser=ser,
        ser_boost=0.15,
        ser_min_confidence=0.60,
    )


def nlp_threat(score, severity, classification):
    return SimpleNamespace(
        threat_score=score,
        severity_level=severity,
        classification=classification,
    )


async def run(pipeline, seeded, scream_confidence=0.2, is_scream=False):
    return await pipeline.process_stored_audio(
        db=seeded.db,
        audio_bytes=b"fake-wav-bytes",
        event=seeded.event,
        clip=seeded.clip,
        location_id="loc-1",
        scream_confidence=scream_confidence,
        is_scream=is_scream,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_angry_high_confidence_boosts_score_and_fires_alert(seeded):
    """angry @ 0.85 boosts NLP 0.65 → 0.80, crossing the 0.75 threshold."""
    ser = make_ser_mock(ser_result("angry", 0.85))
    pipeline = make_pipeline(
        stt_result={"text": "give me your money now", "language": "en"},
        nlp_result=nlp_threat(0.65, "medium", "verbal_threat"),
        ser=ser,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    # Alert fires only because of the SER boost (0.65 < 0.75 <= 0.80)
    assert result["alert_fired"] is True
    assert result["alert_id"] is not None
    assert result["emotion"] == "angry"
    assert result["emotion_confidence"] == 0.85
    # final_score = max(scream_confidence=0.2, boosted=0.80)
    assert result["threat_score"] == pytest.approx(0.80)

    # SER ran on the same temp WAV used for STT
    ser.analyse.assert_awaited_once()

    # Analysis row stores the BOOSTED score
    analysis = seeded.db.query(Analysis).one()
    assert analysis.threat_score == pytest.approx(0.80)

    # EmotionAnalysis row persisted
    row = seeded.db.query(EmotionAnalysis).filter(
        EmotionAnalysis.event_id == seeded.event.event_id
    ).one()
    assert row.emotion == "angry"
    assert row.confidence == pytest.approx(0.85)

    # Alert row exists
    assert seeded.db.query(Alert).count() == 1

    # WS broadcast carries the emotion and the boosted score
    pipeline.ws.broadcast_alert.assert_awaited_once()
    ws_kwargs = pipeline.ws.broadcast_alert.await_args.kwargs
    assert ws_kwargs["emotion"] == "angry"
    assert ws_kwargs["emotion_confidence"] == 0.85
    assert ws_kwargs["threat_score"] == pytest.approx(0.80)

    # MQTT fan-out carries the emotion too
    mqtt_kwargs = pipeline.mqtt.publish_alert.call_args.kwargs
    assert mqtt_kwargs["emotion"] == "angry"


@pytest.mark.asyncio
async def test_neutral_emotion_no_boost_no_alert(seeded):
    """neutral is not a negative emotion — no boost, 0.65 stays below 0.75."""
    ser = make_ser_mock(ser_result("neutral", 0.79))
    pipeline = make_pipeline(
        stt_result={"text": "see you at lunch", "language": "en"},
        nlp_result=nlp_threat(0.65, "medium", "normal"),
        ser=ser,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    assert result["alert_fired"] is False
    assert result["alert_id"] is None
    assert result["emotion"] == "neutral"
    assert seeded.db.query(Alert).count() == 0

    # Analysis keeps the raw NLP score
    analysis = seeded.db.query(Analysis).one()
    assert analysis.threat_score == pytest.approx(0.65)

    # EmotionAnalysis row is STILL written (persisted for every SER result)
    row = seeded.db.query(EmotionAnalysis).one()
    assert row.emotion == "neutral"
    assert row.confidence == pytest.approx(0.79)

    pipeline.ws.broadcast_alert.assert_not_awaited()
    pipeline.mqtt.publish_alert.assert_not_called()


@pytest.mark.asyncio
async def test_angry_below_min_confidence_no_boost(seeded):
    """angry @ 0.5 < ser_min_confidence 0.60 — no boost, no alert, row written."""
    ser = make_ser_mock(ser_result("angry", 0.5))
    pipeline = make_pipeline(
        stt_result={"text": "whatever man", "language": "en"},
        nlp_result=nlp_threat(0.65, "medium", "normal"),
        ser=ser,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    assert result["alert_fired"] is False
    assert result["emotion"] == "angry"
    assert seeded.db.query(Alert).count() == 0

    analysis = seeded.db.query(Analysis).one()
    assert analysis.threat_score == pytest.approx(0.65)  # unboosted

    # Row persisted even though the boost was not applied
    row = seeded.db.query(EmotionAnalysis).one()
    assert row.emotion == "angry"
    assert row.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_ser_failure_returns_none_pipeline_unaffected(seeded):
    """SER returning None must be identical to no-SER: NLP 0.9 alert still fires."""
    ser = make_ser_mock(None)
    pipeline = make_pipeline(
        stt_result={"text": "I will beat you up", "language": "en"},
        nlp_result=nlp_threat(0.9, "high", "verbal_threat"),
        ser=ser,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    assert result["alert_fired"] is True
    assert result["emotion"] is None
    assert result["emotion_confidence"] is None
    assert result["threat_score"] == pytest.approx(0.9)

    ser.analyse.assert_awaited_once()

    # No EmotionAnalysis row when SER fails
    assert seeded.db.query(EmotionAnalysis).count() == 0

    analysis = seeded.db.query(Analysis).one()
    assert analysis.threat_score == pytest.approx(0.9)
    assert seeded.db.query(Alert).count() == 1

    ws_kwargs = pipeline.ws.broadcast_alert.await_args.kwargs
    assert ws_kwargs["emotion"] is None
    assert ws_kwargs["emotion_confidence"] is None


@pytest.mark.asyncio
async def test_ser_disabled_pipeline_works_as_before(seeded):
    """ser=None (disabled) — pipeline behaves exactly as pre-SER."""
    pipeline = make_pipeline(
        stt_result={"text": "I will beat you up", "language": "en"},
        nlp_result=nlp_threat(0.9, "high", "verbal_threat"),
        ser=None,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    assert result["alert_fired"] is True
    assert result["emotion"] is None
    assert result["emotion_confidence"] is None
    assert result["threat_score"] == pytest.approx(0.9)

    assert seeded.db.query(EmotionAnalysis).count() == 0
    assert seeded.db.query(Alert).count() == 1

    # WS payload still includes the emotion kwargs, valued None
    ws_kwargs = pipeline.ws.broadcast_alert.await_args.kwargs
    assert ws_kwargs["emotion"] is None
    assert ws_kwargs["emotion_confidence"] is None


@pytest.mark.asyncio
async def test_boost_is_capped_at_one(seeded):
    """NLP 0.95 + 0.15 boost must cap at 1.0 in Analysis, dict and WS payload."""
    ser = make_ser_mock(ser_result("angry", 0.95))
    pipeline = make_pipeline(
        stt_result={"text": "I am going to kill you", "language": "en"},
        nlp_result=nlp_threat(0.95, "high", "verbal_threat"),
        ser=ser,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    assert result["alert_fired"] is True
    assert result["threat_score"] == pytest.approx(1.0)

    analysis = seeded.db.query(Analysis).one()
    assert analysis.threat_score == pytest.approx(1.0)

    ws_kwargs = pipeline.ws.broadcast_alert.await_args.kwargs
    assert ws_kwargs["threat_score"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_fearful_high_confidence_also_boosts(seeded):
    """fearful is the second negative emotion — boost applies the same way."""
    ser = make_ser_mock(ser_result("fearful", 0.70))
    pipeline = make_pipeline(
        stt_result={"text": "please leave me alone", "language": "en"},
        nlp_result=nlp_threat(0.65, "medium", "distress"),
        ser=ser,
    )

    result = await run(pipeline, seeded, scream_confidence=0.2, is_scream=False)

    assert result["alert_fired"] is True
    assert result["threat_score"] == pytest.approx(0.80)
    analysis = seeded.db.query(Analysis).one()
    assert analysis.threat_score == pytest.approx(0.80)
    row = seeded.db.query(EmotionAnalysis).one()
    assert row.emotion == "fearful"
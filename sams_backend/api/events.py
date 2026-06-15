"""
api/events.py
═══════════════════════════════════════════════════════
API CONTRACT for Lee Jia Shin's edge device
═══════════════════════════════════════════════════════

POST /api/events/audio
  Lee's ESP32-C3 calls this after her scream detection triggers.
  Sends: multipart/form-data with audio file + metadata fields.

GET  /api/events/{event_id}/audio
  Dashboard calls this to stream the audio clip for playback.
"""
import logging
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from models.database import Event, AudioClip
from models.schemas import ProcessingResponse
from api.dependencies import get_db, get_pipeline, get_audio_capture

router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
logger = logging.getLogger(__name__)


@router.post(
    "/audio",
    response_model=ProcessingResponse,
    summary="[Lee's ESP32] Submit audio clip for processing",
)
async def receive_audio_event(
    # ── Fields Lee's ESP32 must send ─────────────────────────────────────────
    device_id:        str   = Form(..., description="ESP32 device ID, e.g. esp32-001"),
    location_id:      str   = Form(..., description="Physical location ID"),
    timestamp:        str   = Form(..., description="ISO8601 UTC, e.g. 2026-06-11T10:30:00"),
    intensity:        float = Form(..., description="Sound intensity in dB from edge"),
    pitch:            float = Form(..., description="Dominant pitch in Hz from edge"),
    confidence_score: float = Form(..., description="Edge scream classifier confidence 0–1"),
    duration_seconds: float = Form(..., description="Audio clip length in seconds"),
    audio_file: UploadFile   = File(..., description="WAV audio clip (~5–10 seconds)"),
    # ── Injected ─────────────────────────────────────────────────────────────
    db:       Session = Depends(get_db),
    pipeline            = Depends(get_pipeline),
):
    """
    Entry point for Lee's edge device.

    The ESP32 sends this after its onboard scream detection fires.
    Your cloud then runs Module 1 (VAD + capture) → Module 2 (STT + NLP)
    and pushes an alert to the dashboard if the threat score is high enough.
    """
    audio_bytes = await audio_file.read()

    result = await pipeline.process(
        db              = db,
        audio_bytes     = audio_bytes,
        filename        = audio_file.filename or "audio.wav",
        device_id       = device_id,
        location_id     = location_id,
        timestamp_str   = timestamp,
        intensity       = intensity,
        pitch           = pitch,
        edge_confidence = confidence_score,
        duration_hint   = duration_seconds,
    )
    return result


@router.get(
    "/{event_id}/audio",
    summary="Stream audio clip for dashboard playback",
)
async def stream_audio(
    event_id: str,
    db:       Session = Depends(get_db),
    storage             = Depends(get_audio_capture),
):
    """Dashboard calls this to play back the audio for an incident."""
    event = db.query(Event).filter(Event.event_id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")

    clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
    if not clip or not clip.file_path:
        raise HTTPException(404, "Audio clip not found")

    return FileResponse(
        clip.file_path,
        media_type = "audio/wav",
        filename   = f"incident_{event_id}.wav",
    )

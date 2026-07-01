

# """
# api/events.py
# ═══════════════════════════════════════════════════════
# API CONTRACT for Lee Jia Shin's edge device
# ═══════════════════════════════════════════════════════

# POST /api/events/audio
#   Lee's ESP32-C3 calls this after her scream detection triggers.
#   Sends: multipart/form-data with audio file + metadata fields.

# GET  /api/events/{event_id}/audio
#   Dashboard calls this to stream the audio clip for playback.
# """
# import io
# import os
# import logging
# from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
# from fastapi.responses import FileResponse, StreamingResponse
# from sqlalchemy.orm import Session

# from models.database import Event, AudioClip
# from models.schemas import ProcessingResponse
# from api.dependencies import get_db, get_pipeline, get_audio_storage

# router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
# logger = logging.getLogger(__name__)


# @router.post(
#     "/audio",
#     response_model=ProcessingResponse,
#     summary="[Lee's ESP32] Submit audio clip for processing",
# )
# async def receive_audio_event(
#     # ── Fields Lee's ESP32 must send ─────────────────────────────────────────
#     device_id:        str   = Form(..., description="ESP32 device ID, e.g. esp32-001"),
#     location_id:      str   = Form(..., description="Physical location ID"),
#     timestamp:        str   = Form(..., description="ISO8601 UTC, e.g. 2026-06-11T10:30:00"),
#     intensity:        float = Form(..., description="Sound intensity in dB from edge"),
#     pitch:            float = Form(..., description="Dominant pitch in Hz from edge"),
#     confidence_score: float = Form(..., description="Edge scream classifier confidence 0–1"),
#     duration_seconds: float = Form(..., description="Audio clip length in seconds"),
#     audio_file: UploadFile   = File(..., description="WAV audio clip (~5–10 seconds)"),
#     # ── Injected ─────────────────────────────────────────────────────────────
#     db:       Session = Depends(get_db),
#     pipeline            = Depends(get_pipeline),
# ):
#     """
#     Entry point for Lee's edge device.

#     The ESP32 sends this after its onboard scream detection fires.
#     Your cloud then runs Module 1 (VAD + capture) → Module 2 (STT + NLP)
#     and pushes an alert to the dashboard if the threat score is high enough.
#     """
#     audio_bytes = await audio_file.read()

#     result = await pipeline.process(
#         db              = db,
#         audio_bytes     = audio_bytes,
#         filename        = audio_file.filename or "audio.wav",
#         device_id       = device_id,
#         location_id     = location_id,
#         timestamp_str   = timestamp,
#         intensity       = intensity,
#         pitch           = pitch,
#         edge_confidence = confidence_score,
#         duration_hint   = duration_seconds,
#     )
#     return result


# @router.get(
#     "/{event_id}/audio",
#     summary="Stream audio clip for dashboard playback",
# )
# async def stream_audio(
#     event_id: str,
#     db:       Session = Depends(get_db),
#     storage             = Depends(get_audio_storage),
# ):
#     """Dashboard calls this to play back the audio for an incident.

#     Serves the clip from Supabase Storage when it was uploaded there, otherwise
#     streams the local file — transparent to the caller either way.
#     """
#     event = db.query(Event).filter(Event.event_id == event_id).first()
#     if not event:
#         raise HTTPException(404, "Event not found")

#     clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
#     if not clip or not clip.file_path:
#         raise HTTPException(404, "Audio clip not found")

#     # Remote (Supabase) clip → fetch bytes and stream them through our endpoint.
#     if storage.is_remote(clip.file_path):
#         data = storage.get_bytes(clip.file_path)
#         if data is None:
#             raise HTTPException(404, "Audio clip not found")
#         return StreamingResponse(
#             io.BytesIO(data),
#             media_type = "audio/wav",
#             headers    = {"Content-Disposition": f'inline; filename="incident_{event_id}.wav"'},
#         )

#     # Local clip → serve straight off disk.
#     if not os.path.exists(clip.file_path):
#         raise HTTPException(404, "Audio clip not found")
#     return FileResponse(
#         clip.file_path,
#         media_type = "audio/wav",
#         filename   = f"incident_{event_id}.wav",
#     )

"""
api/events.py
═══════════════════════════════════════════════════════
MERGED SUPABASE CONTRACT — Scream Analysis (You) + NLP Pipeline (Teammate)
═══════════════════════════════════════════════════════
"""
import logging
import uuid
import io
import os
import tempfile
from datetime import datetime
from fastapi import APIRouter, Body, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from supabase import create_client, Client
from config.settings import get_settings

from models.database import Event, AudioClip, Device, Location, Alert
from models.schemas import ProcessingResponse
from api.dependencies import (
    get_db,
    get_pipeline,
    get_ws_manager,
    get_audio_storage,
)
from services.storage_service import AudioStorageService
from services.audio_capture_service import AudioCaptureService
from services.scream_analyzer import ScreamAnalyzer

router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
logger = logging.getLogger(__name__)

# Initialize your Scream Analyzer
_analyzer = ScreamAnalyzer()


# ────────────────────────────────────────────────────────────────
# Load Supabase configuration from .env
# ────────────────────────────────────────────────────────────────
# cfg = get_settings()
# supabase_client: Client = create_client(
#     cfg.supabase_url,
#     cfg.supabase_service_key
# )
# BUCKET_NAME = cfg.supabase_bucket


@router.post(
    "/audio",
    summary="[ESP32] Notify backend after uploading audio to Supabase",
)
async def receive_audio_event(
    device_id: str = Form(...),
    location_id: str = Form(...),
    timestamp: str = Form(...),
    sound_level: str = Form("0"),
    duration_seconds: str = Form("8"),
    # ⬇️ IMPORTANT: Add this field!
    supabase_file_path: str = Form(...),  # The filename uploaded to Supabase
    db: Session = Depends(get_db),
    audio_storage: AudioStorageService = Depends(get_audio_storage),
):
    """
    ESP32 calls this after uploading audio to Supabase.
    Backend downloads the audio, runs scream detection, saves to DB.
    """
    try:
        logger.info(f"[Audio] Received notification from {device_id} for file: {supabase_file_path}")
        
        # ── Step 1: Download audio from Supabase ──────────────────────────────
        try:
            # The file is stored as just the UUID filename (without folder)
            # Since your ESP32 uploads to root of bucket
            audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{supabase_file_path}")
            if not audio_bytes:
                raise ValueError("Downloaded file is empty")
            logger.info(f"[Audio] Downloaded {len(audio_bytes):,} bytes from {supabase_file_path}")
        except Exception as e:
            logger.error(f"[Audio] Failed to download from Supabase: {e}")
            # Try with "incidents/" prefix as fallback
            try:
                audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/incidents/{supabase_file_path}")
                if not audio_bytes:
                    raise ValueError("Downloaded file is empty")
                logger.info(f"[Audio] Downloaded {len(audio_bytes):,} bytes from incidents/{supabase_file_path}")
            except Exception as e2:
                logger.error(f"[Audio] Both download attempts failed: {e2}")
                return {
                    "status": "error", 
                    "message": f"Supabase download error: {str(e)}"
                }
        
        # ── Step 2: Run scream analysis ──────────────────────────────────────────
        result = _analyzer.analyze(audio_bytes)
        
        if result.get('error'):
            logger.error(f"[Audio] Analysis error: {result['error']}")
            return {"status": "error", "message": result['error']}
        
        is_scream = result.get('is_scream', False)
        confidence = result.get('confidence', 0.0)
        
        logger.info(f"[Audio] Analysis result: is_scream={is_scream}, confidence={confidence:.3f}")
        
        # ── Step 3: Parse timestamp ──────────────────────────────────────────────
        try:
            event_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except:
            event_timestamp = datetime.utcnow()
        
        # ── Step 4: Save to database ──────────────────────────────────────────────
        event_id = str(uuid.uuid4())
        
        # Ensure device exists
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device:
            device = Device(
                device_id=device_id,
                location_id=location_id,
                status="online"
            )
            db.add(device)
            db.commit()
            db.refresh(device)
        
        # Create event
        event = Event(
            event_id=event_id,
            device_id=device_id,
            timestamp=event_timestamp,
            intensity=float(sound_level) if sound_level else 0.0,
            pitch=0.0,
            confidence_score=confidence
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        
        # Save audio clip reference
        clip = AudioClip(
            clip_id=str(uuid.uuid4()),
            event_id=event_id,
            file_path=supabase_file_path,  # Store the Supabase path
            duration=float(duration_seconds) if duration_seconds else 8.0
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)
        
        # ── Step 5: If scream detected, create alert and broadcast ──────────────
        alert_fired = False
        alert_id = None
        
        if is_scream:
            severity = "high" if confidence > 0.7 else ("medium" if confidence > 0.4 else "low")
            
            alert_id = str(uuid.uuid4())
            alert = Alert(
                alert_id=alert_id,
                event_id=event_id,
                severity=severity,
                status="active",
                created_at=datetime.utcnow()
            )
            db.add(alert)
            db.commit()
            db.refresh(alert)
            alert_fired = True
            
            location = db.query(Location).filter(Location.location_id == location_id).first()
            location_name = location.location_name if location else location_id
            
            logger.warning(f"[Audio] 🚨 ALERT FIRED! severity={severity}, confidence={confidence:.3f}")
            
            # Broadcast via WebSocket
            try:
                ws_manager = get_ws_manager()
                await ws_manager.broadcast_alert(
                    alert_id=alert_id,
                    event_id=event_id,
                    location_name=location_name,
                    severity=severity,
                    threat_score=confidence,
                    classification="scream" if is_scream else "noise",
                    transcript=f"Scream detected with {confidence:.1%} confidence",
                    audio_url=f"/api/events/{event_id}/audio",
                    timestamp=event_timestamp.isoformat()
                )
                logger.info(f"[Audio] WebSocket broadcast sent")
            except Exception as e:
                logger.error(f"[Audio] WebSocket broadcast failed: {e}")
        
        # ── Step 6: Return response to ESP32 ──────────────────────────────────────
        return {
            "status": "success",
            "event_id": event_id,
            "is_scream": is_scream,
            "confidence": confidence,
            "alert_fired": alert_fired,
            "alert_id": alert_id,
            "message": "Scream detected!" if is_scream else "No scream detected"
        }
        
    except Exception as e:
        logger.error(f"[Audio] Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Processing error: {str(e)}")


@router.get("/{event_id}/audio", summary="Stream audio clip straight out of Supabase Storage")
async def stream_audio(event_id: str, db: Session = Depends(get_db), audio_storage: AudioStorageService = Depends(get_audio_storage)):
    """Pipes the file from Supabase right down to the dashboard browser player."""
    clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
    if not clip or not clip.file_path:
        raise HTTPException(404, "Audio file reference context missing")

    file_path = clip.file_path

    # ── Old local records (Windows backslash path) — serve from disk ──────
    if "\\" in file_path or (not file_path.startswith("supabase://") and not file_path.startswith("incidents/")):
        from fastapi.responses import FileResponse
        if not os.path.exists(file_path):
            raise HTTPException(404, "Local audio file no longer exists")
        return FileResponse(file_path, media_type="audio/wav", filename=f"{event_id}.wav")

    # ── Resolve to a plain object key ─────────────────────────────────────
    # Handles three stored formats:
    #   "supabase://audio-clips/incidents/<uuid>.wav"  → "incidents/<uuid>.wav"
    #   "supabase://audio-clips/<uuid>.wav"            → "<uuid>.wav"
    #   "<uuid>.wav"  or  "incidents/<uuid>.wav"       → used as-is
    if file_path.startswith("supabase://"):
        object_key = file_path.split("/", 3)[-1]   # strip "supabase://audio-clips/"
    else:
        object_key = file_path                      # already a plain key

    try:
        audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{object_key}")
        if not audio_bytes:
            raise HTTPException(404, "Audio file not found in Supabase")
        return StreamingResponse(
            io.BytesIO(audio_bytes),
            media_type="audio/wav",
            headers={"Content-Disposition": f"attachment; filename={event_id}.wav"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error streaming from Supabase: {e}")
        raise HTTPException(500, "Failed to retrieve audio")


@router.get(
    "/all",
    summary="Get all events for the Scream Alerts dashboard tab",
)
async def get_all_events(
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Returns all historic events from tracking database, ordered chronologically descending."""
    try:
        events = (
            db.query(Event)
            .order_by(desc(Event.timestamp))
            .limit(limit)
            .all()
        )
        
        result = []
        for event in events:
            device = db.query(Device).filter(Device.device_id == event.device_id).first()
            location_name = "Unknown"
            location_id = "Unknown"
            
            if device and device.location_id:
                location_id = device.location_id
                location = db.query(Location).filter(Location.location_id == device.location_id).first()
                if location:
                    location_name = location.location_name
            
            result.append({
                "id": event.event_id,
                "device_id": event.device_id or "Unknown",
                "location_id": location_id,
                "location_name": location_name,
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "intensity": event.intensity or 0,
                "pitch": event.pitch or 0,
                "confidence_score": event.confidence_score or 0,
            })
        
        return {"events": result}
        
    except Exception as e:
        logger.error(f"Error fetching all events: {e}")
        return {"events": []}


@router.get(
    "/stats",
    summary="Get event statistics for the dashboard",
)
async def get_event_stats(
    db: Session = Depends(get_db),
):
    """Calculates status overview metric counters for dashboard view displays."""
    try:
        total = db.query(Event).count()
        high = db.query(Event).filter(Event.confidence_score > 0.7).count()
        medium = db.query(Event).filter(Event.confidence_score > 0.4, Event.confidence_score <= 0.7).count()
        low = db.query(Event).filter(Event.confidence_score <= 0.4).count()
        screams = db.query(Event).filter(Event.confidence_score > 0.5).count()
        
        return {
            "total": total,
            "high": high,
            "medium": medium,
            "low": low,
            "screams": screams
        }
    except Exception as e:
        logger.error(f"Error getting event stats: {e}")
        return {"total": 0, "high": 0, "medium": 0, "low": 0, "screams": 0}



# Add to events.py - Webhook endpoint for Supabase Storage events

@router.post(
    "/webhook/supabase-storage",
    summary="[Webhook] Triggered by Supabase when new audio is uploaded"
)
async def supabase_storage_webhook(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    audio_storage: AudioStorageService = Depends(get_audio_storage)
):
    """
    Called by Supabase Storage webhook when a new file is uploaded.
    Downloads the file, runs scream detection, saves results to DB.
    """
    try:
        # ── Extract file info from webhook payload ──────────────────────────
        # Supabase sends: { "type": "INSERT", "record": { "name": "...", "bucket_id": "...", ... } }
        record = payload.get("record", {})
        file_name = record.get("name")
        bucket_name = record.get("bucket_id", "audio-clips")
        
        if not file_name:
            raise HTTPException(400, "No file name in webhook payload")
        
        logger.info(f"[Webhook] New file uploaded: {file_name} to bucket: {bucket_name}")
        
        # ── Check if this is an audio file ───────────────────────────────────
        if not file_name.endswith('.wav'):
            logger.info(f"[Webhook] Skipping non-audio file: {file_name}")
            return {"status": "skipped", "reason": "Not a WAV file"}
        
        # ── Extract device info from filename ────────────────────────────────
        # Filename format: esp32-001_20260629T155300Z.wav
        # OR: 1ea33c87-35db-40a3-903e-d1e512fe5c4a.wav (UUID)
        device_id = "esp32-001"  # Default fallback
        location_id = "loc-toilet-a"
        
        # Try to extract device ID from filename if using old format
        if file_name.startswith("esp32-"):
            parts = file_name.split('_')
            if len(parts) >= 1:
                device_id = parts[0]
        
        # ── Download audio from Supabase ──────────────────────────────────────
        try:
            audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{file_name}")
            if not audio_bytes:
                raise ValueError("Downloaded file is empty")
            logger.info(f"[Webhook] Downloaded {len(audio_bytes):,} bytes from {file_name}")
        except Exception as e:
            logger.error(f"[Webhook] Failed to download from Supabase: {e}")
            raise HTTPException(500, f"Supabase download error: {str(e)}")
        
        # ── Run scream analysis ──────────────────────────────────────────────
        result = _analyzer.analyze(audio_bytes)
        
        if result.get('error'):
            logger.error(f"[Webhook] Analysis error: {result['error']}")
            return {"status": "error", "message": result['error']}
        
        is_scream = result.get('is_scream', False)
        confidence = result.get('confidence', 0.0)
        
        logger.info(f"[Webhook] Analysis result: is_scream={is_scream}, confidence={confidence:.3f}")
        
        # ── Extract timestamp from filename OR use current time ──────────────
        # If filename is UUID, we need to get timestamp from file metadata
        # For now, use current time
        event_timestamp = datetime.utcnow()
        
        # ── Save to database ──────────────────────────────────────────────────
        event_id = str(uuid.uuid4())
        
        # Ensure device exists
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device:
            device = Device(
                device_id=device_id,
                location_id=location_id,
                status="online"
            )
            db.add(device)
            db.commit()
            db.refresh(device)
        
        # Create event
        event = Event(
            event_id=event_id,
            device_id=device_id,
            timestamp=event_timestamp,
            intensity=0.0,  # Not calculated on ESP
            pitch=0.0,      # Not calculated on ESP
            confidence_score=confidence
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        
        # Save audio clip reference
        clip = AudioClip(
            clip_id=str(uuid.uuid4()),
            event_id=event_id,
            file_path=file_name,  # Supabase path
            duration=0.0
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)
        
        # ── If scream detected, create alert and broadcast ──────────────────
        alert_fired = False
        alert_id = None
        
        if is_scream:
            severity = "high" if confidence > 0.7 else ("medium" if confidence > 0.4 else "low")
            
            alert_id = str(uuid.uuid4())
            alert = Alert(
                alert_id=alert_id,
                event_id=event_id,
                severity=severity,
                status="active",
                created_at=datetime.utcnow()
            )
            db.add(alert)
            db.commit()
            db.refresh(alert)
            alert_fired = True
            
            location = db.query(Location).filter(Location.location_id == location_id).first()
            location_name = location.location_name if location else location_id
            
            logger.warning(f"[Webhook] 🚨 ALERT FIRED! severity={severity}, confidence={confidence:.3f}")
            
            # Broadcast via WebSocket
            try:
                ws_manager = get_ws_manager()
                await ws_manager.broadcast_alert(
                    alert_id=alert_id,
                    event_id=event_id,
                    location_name=location_name,
                    severity=severity,
                    threat_score=confidence,
                    classification="scream" if is_scream else "noise",
                    transcript=f"Scream detected with {confidence:.1%} confidence",
                    audio_url=f"/api/events/{event_id}/audio",
                    timestamp=event_timestamp.isoformat()
                )
                logger.info(f"[Webhook] WebSocket broadcast sent")
            except Exception as e:
                logger.error(f"[Webhook] WebSocket broadcast failed: {e}")
        
        return {
            "status": "success",
            "event_id": event_id,
            "is_scream": is_scream,
            "confidence": confidence,
            "alert_fired": alert_fired,
            "alert_id": alert_id,
            "message": "Scream detected!" if is_scream else "No scream detected"
        }
        
    except Exception as e:
        logger.error(f"[Webhook] Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Webhook processing error: {str(e)}")

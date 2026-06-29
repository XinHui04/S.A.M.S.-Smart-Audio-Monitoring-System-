# # """
# # api/events.py
# # ═══════════════════════════════════════════════════════
# # API CONTRACT for Lee Jia Shin's edge device
# # ═══════════════════════════════════════════════════════

# # POST /api/events/audio
# #   Lee's ESP32-C3 calls this after her scream detection triggers.
# #   Sends: multipart/form-data with audio file + metadata fields.

# # GET  /api/events/{event_id}/audio
# #   Dashboard calls this to stream the audio clip for playback.

# # GET  /api/events/all  ← NEW: Get all events for the Scream Alerts tab
# # """
# # import logging
# # from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
# # from fastapi.responses import FileResponse
# # from sqlalchemy.orm import Session
# # from sqlalchemy import desc

# # from models.database import Event, AudioClip, Device, Location
# # from models.schemas import ProcessingResponse
# # from api.dependencies import get_db, get_pipeline, get_audio_capture

# # router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
# # logger = logging.getLogger(__name__)


# # @router.post(
# #     "/audio",
# #     response_model=ProcessingResponse,
# #     summary="[Lee's ESP32] Submit audio clip for processing",
# # )
# # async def receive_audio_event(
# #     # ── Fields Lee's ESP32 must send ─────────────────────────────────────────
# #     device_id:        str   = Form(..., description="ESP32 device ID, e.g. esp32-001"),
# #     location_id:      str   = Form(..., description="Physical location ID"),
# #     timestamp:        str   = Form(..., description="ISO8601 UTC, e.g. 2026-06-11T10:30:00"),
# #     intensity:        float = Form(..., description="Sound intensity in dB from edge"),
# #     pitch:            float = Form(..., description="Dominant pitch in Hz from edge"),
# #     confidence_score: float = Form(..., description="Edge scream classifier confidence 0–1"),
# #     duration_seconds: float = Form(..., description="Audio clip length in seconds"),
# #     audio_file: UploadFile   = File(..., description="WAV audio clip (~5–10 seconds)"),
# #     # ── Injected ─────────────────────────────────────────────────────────────
# #     db:       Session = Depends(get_db),
# #     pipeline            = Depends(get_pipeline),
# # ):
# #     """
# #     Entry point for Lee's edge device.

# #     The ESP32 sends this after its onboard scream detection fires.
# #     Your cloud then runs Module 1 (VAD + capture) → Module 2 (STT + NLP)
# #     and pushes an alert to the dashboard if the threat score is high enough.
# #     """
# #     audio_bytes = await audio_file.read()

# #     result = await pipeline.process(
# #         db              = db,
# #         audio_bytes     = audio_bytes,
# #         filename        = audio_file.filename or "audio.wav",
# #         device_id       = device_id,
# #         location_id     = location_id,
# #         timestamp_str   = timestamp,
# #         intensity       = intensity,
# #         pitch           = pitch,
# #         edge_confidence = confidence_score,
# #         duration_hint   = duration_seconds,
# #     )
# #     return result


# # @router.get(
# #     "/{event_id}/audio",
# #     summary="Stream audio clip for dashboard playback",
# # )
# # async def stream_audio(
# #     event_id: str,
# #     db:       Session = Depends(get_db),
# #     storage             = Depends(get_audio_capture),
# # ):
# #     """Dashboard calls this to play back the audio for an incident."""
# #     event = db.query(Event).filter(Event.event_id == event_id).first()
# #     if not event:
# #         raise HTTPException(404, "Event not found")

# #     clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
# #     if not clip or not clip.file_path:
# #         raise HTTPException(404, "Audio clip not found")

# #     return FileResponse(
# #         clip.file_path,
# #         media_type = "audio/wav",
# #         filename   = f"incident_{event_id}.wav",
# #     )

# # # ── NEW: Get all events for Scream Alerts tab ──────────────────────────────────
# # @router.get(
# #     "/all",
# #     summary="Get all events for the Scream Alerts dashboard tab",
# # )
# # async def get_all_events(
# #     limit: int = 100,
# #     db: Session = Depends(get_db),
# # ):
# #     """
# #     Returns all events from the database, ordered by timestamp descending.
# #     Used by the Scream Alerts tab in the dashboard.
# #     """
# #     try:
# #         # Query events with joins to get device and location info
# #         events = (
# #             db.query(Event)
# #             .order_by(desc(Event.timestamp))
# #             .limit(limit)
# #             .all()
# #         )
        
# #         result = []
# #         for event in events:
# #             # Get location name through device
# #             device = db.query(Device).filter(Device.device_id == event.device_id).first()
# #             location_name = "Unknown"
# #             if device and device.location_id:
# #                 location = db.query(Location).filter(Location.location_id == device.location_id).first()
# #                 if location:
# #                     location_name = location.location_name
            
# #             result.append({
# #                 "id": event.event_id,
# #                 "device_id": event.device_id or "Unknown",
# #                 "location_id": device.location_id if device else "Unknown",
# #                 "location_name": location_name,
# #                 "timestamp": event.timestamp.isoformat() if event.timestamp else None,
# #                 "intensity": event.intensity or 0,
# #                 "pitch": event.pitch or 0,
# #                 "confidence_score": event.confidence_score or 0,
# #             })
        
# #         return {"events": result}
        
# #     except Exception as e:
# #         logger.error(f"Error fetching all events: {e}")
# #         return {"events": []}


# # # ── NEW: Get event stats for Scream Alerts tab ──────────────────────────────────
# # @router.get(
# #     "/stats",
# #     summary="Get event statistics for the dashboard",
# # )
# # async def get_event_stats(
# #     db: Session = Depends(get_db),
# # ):
# #     """
# #     Returns statistics about events (total, high/medium/low confidence).
# #     """
# #     try:
# #         total = db.query(Event).count()
        
# #         # High confidence (> 0.7)
# #         high = db.query(Event).filter(Event.confidence_score > 0.7).count()
        
# #         # Medium confidence (0.4 - 0.7)
# #         medium = db.query(Event).filter(
# #             Event.confidence_score > 0.4,
# #             Event.confidence_score <= 0.7
# #         ).count()
        
# #         # Low confidence (<= 0.4)
# #         low = db.query(Event).filter(Event.confidence_score <= 0.4).count()
        
# #         # Screams (confidence > 0.5)
# #         screams = db.query(Event).filter(Event.confidence_score > 0.5).count()
        
# #         return {
# #             "total": total,
# #             "high": high,
# #             "medium": medium,
# #             "low": low,
# #             "screams": screams
# #         }
        
# #     except Exception as e:
# #         logger.error(f"Error getting event stats: {e}")
# #         return {
# #             "total": 0,
# #             "high": 0,
# #             "medium": 0,
# #             "low": 0,
# #             "screams": 0
# #         }



# """
# api/events.py
# ═══════════════════════════════════════════════════════
# API CONTRACT for Lee Jia Shin's edge device
# ═══════════════════════════════════════════════════════

# POST /api/events/audio/detect
#   ESP32-C3 sends raw audio for cloud-based scream detection.
#   Sends: multipart/form-data with audio file + basic metadata.
#   Returns: confidence_score, is_scream, alert_fired

# GET  /api/events/{event_id}/audio
#   Dashboard calls this to stream the audio clip for playback.

# GET  /api/events/all
#   Get all events for the Scream Alerts tab

# GET  /api/events/stats
#   Get event statistics for the dashboard
# """
# import logging
# import uuid
# from datetime import datetime
# from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
# from fastapi.responses import FileResponse
# from sqlalchemy.orm import Session
# from sqlalchemy import desc

# from models.database import Event, AudioClip, Device, Location, Alert
# from models.schemas import ProcessingResponse
# from api.dependencies import get_db, get_pipeline, get_audio_capture, get_ws_manager
# from services.scream_analyzer import ScreamAnalyzer

# router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
# logger = logging.getLogger(__name__)

# # ── Initialize Scream Analyzer (once at startup) ──────────────────────────
# _analyzer = ScreamAnalyzer()


# @router.post("/debug")
# async def debug_form(
#     device_id: str = Form(...),
#     location_id: str = Form(...),
#     timestamp: str = Form(...),
#     sound_level: str = Form(...),
#     duration_seconds: str = Form(...),
#     audio_file: UploadFile = File(...),
# ):
#     """Debug endpoint to see what's being sent"""
#     try:
#         audio_bytes = await audio_file.read()
#         return {
#             "status": "debug",
#             "device_id": device_id,
#             "location_id": location_id,
#             "timestamp": timestamp,
#             "sound_level": sound_level,
#             "duration_seconds": duration_seconds,
#             "audio_size": len(audio_bytes),
#             "audio_filename": audio_file.filename,
#             "content_type": audio_file.content_type,
#             "message": "Debug data received successfully"
#         }
#     except Exception as e:
#         logger.error(f"Debug error: {e}")
#         raise HTTPException(500, f"Debug error: {str(e)}")

# @router.post(
#     "/audio/detect",
#     summary="[ESP32-C3] Send audio to cloud for scream detection",
# )
# async def detect_scream(
#     # ── Fields from ESP32-C3 ──────────────────────────────────────────────
#     device_id:        str   = Form(..., description="ESP32 device ID, e.g. esp32-001"),
#     location_id:      str   = Form(..., description="Physical location ID"),
#     timestamp:        str   = Form(..., description="ISO8601 UTC, e.g. 2026-06-23T10:30:00"),
#     sound_level:      str   = Form(..., description="Raw sound level from analog sensor"),
#     duration_seconds: str   = Form(..., description="Audio clip length in seconds"),
#     audio_file: UploadFile   = File(..., description="WAV audio clip to analyze"),
#     # ── Injected ─────────────────────────────────────────────────────────────
#     db: Session = Depends(get_db),
# ):
#     """
#     Entry point for ESP32-C3 (without onboard TFLite).

#     The ESP32 sends raw audio to the cloud for scream detection.
#     The cloud runs the scream detection model and returns the result.
#     """
#     try:
#         # ── Convert sound_level from string to int ──────────────────────────
#         try:
#             sound_level_int = int(sound_level)
#         except ValueError:
#             sound_level_int = 0
#             logger.warning(f"[Scream Detect] Invalid sound_level: {sound_level}, using 0")
        
#         logger.info(f"[Scream Detect] Received from {device_id}, sound_level={sound_level_int}")

#         try:
#             duration = float(duration_seconds)
#         except ValueError:
#             duration = 2.0
#             logger.warning(f"[Scream Detect] Invalid duration: {duration_seconds}")
        
#         logger.info(f"[Scream Detect] Received from {device_id}, sound_level={sound_level_int}, duration={duration}")

#         # ── Step 1: Read audio bytes ────────────────────────────────────────
#         audio_bytes = await audio_file.read()
        
#         if not audio_bytes:
#             raise HTTPException(400, "Empty audio file received")
        
#         logger.info(f"[Scream Detect] Received audio from {device_id} ({len(audio_bytes)} bytes)")
        
#         # ── Step 2: Analyze for scream using scream_analyzer.py ─────────────
#         # result = _analyzer.analyze(audio_bytes)
        
#         try:
#             result = _analyzer.analyze(audio_bytes)
#         except Exception as e:
#             import traceback
#             traceback.print_exc()   # prints full stack trace to terminal
#             raise HTTPException(500, f"Analyzer crashed: {str(e)}")

#         if result.get('error'):
#             logger.error(f"[Scream Detect] Analysis error: {result['error']}")
#             raise HTTPException(500, f"Analysis failed: {result['error']}")
        
#         is_scream = result.get('is_scream', False)
#         confidence = result.get('confidence', 0.0)
        
#         logger.info(f"[Scream Detect] Result: is_scream={is_scream}, confidence={confidence:.3f}")
        
#         # ── Step 3: Save to database ────────────────────────────────────────
#         event_id = str(uuid.uuid4())
        
#         # Parse timestamp
#         try:
#             event_timestamp = datetime.fromisoformat(timestamp)
#         except ValueError:
#             event_timestamp = datetime.utcnow()
#             logger.warning(f"[Scream Detect] Invalid timestamp, using current time")
        
#         # Get or create device
#         device = db.query(Device).filter(Device.device_id == device_id).first()
#         if not device:
#             logger.info(f"[Scream Detect] Creating new device: {device_id}")
#             device = Device(
#                 device_id=device_id,
#                 location_id=location_id,
#                 status="online"
#             )
#             db.add(device)
#             db.commit()
#             db.refresh(device)
        
#         # Create event record with confidence from scream_analyzer
#         event = Event(
#             event_id=event_id,
#             device_id=device_id,
#             timestamp=event_timestamp,
#             intensity=0.0,  # Not calculated on ESP
#             pitch=0.0,      # Not calculated on ESP
#             confidence_score=confidence  # ← From scream_analyzer.py
#         )
#         db.add(event)
#         db.commit()
#         db.refresh(event)
        
#         # ── Step 4: Save audio file to storage ──────────────────────────────
#         # clip_id = str(uuid.uuid4())
#         # audio_capture = get_audio_capture()
        
#         # # Save the audio file using the storage service
#         # import numpy as np
#         # import soundfile as sf
#         # import tempfile
#         # import os

#         # # On Windows, delete=True locks the file — use delete=False and clean up manually
#         # tmp_path = None
#         # try:
#         #     with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
#         #         tmp.write(audio_bytes)
#         #         tmp_path = tmp.name
#         #     # File is now closed — soundfile can open it on Windows
#         #     audio, sr = sf.read(tmp_path)
#         #     if audio.dtype != np.float32:
#         #         audio = audio.astype(np.float32)
#         #     file_path = audio_capture._save_wav(audio, event_id)
#         # finally:
#         #     if tmp_path and os.path.exists(tmp_path):
#         #         os.unlink(tmp_path)

#         # clip = AudioClip(
#         #     clip_id=clip_id,
#         #     event_id=event_id,
#         #     file_path=file_path,
#         #     duration=duration
#         # )
#         # db.add(clip)
#         # db.commit()
#         # db.refresh(clip)
        
#         # ── Step 4: Save audio file to storage ──────────────────────────────
#         clip_id = str(uuid.uuid4())

#         # audio_bytes is already a valid WAV — save directly, no decode/re-encode needed
#         import os
#         audio_storage_dir = "./audio_storage"
#         os.makedirs(audio_storage_dir, exist_ok=True)
#         file_path = os.path.join(audio_storage_dir, f"{event_id}.wav")
#         with open(file_path, "wb") as f:
#             f.write(audio_bytes)
#         logger.info(f"[Scream Detect] Audio saved to {file_path} ({len(audio_bytes):,} bytes)")

#         clip = AudioClip(
#             clip_id=clip_id,
#             event_id=event_id,
#             file_path=file_path,
#             duration=duration
#         )
#         db.add(clip)
#         db.commit()
#         db.refresh(clip)

        
#         # ── Step 5: If scream detected, create alert ────────────────────────
#         alert_fired = False
#         alert_id = None
        
#         if is_scream:
#             # Determine severity based on confidence
#             if confidence > 0.7:
#                 severity = "high"
#             elif confidence > 0.4:
#                 severity = "medium"
#             else:
#                 severity = "low"
            
#             # Create alert
#             alert_id = str(uuid.uuid4())
#             alert = Alert(
#                 alert_id=alert_id,
#                 event_id=event_id,
#                 severity=severity,
#                 status="active",
#                 created_at=datetime.utcnow()
#             )
#             db.add(alert)
#             db.commit()
#             db.refresh(alert)
#             alert_fired = True
            
#             # Get location name
#             location = db.query(Location).filter(Location.location_id == location_id).first()
#             location_name = location.location_name if location else location_id
            
#             logger.warning(
#                 f"[Scream Detect] 🚨 ALERT FIRED! "
#                 f"severity={severity}, confidence={confidence:.3f}, "
#                 f"location={location_name}, device={device_id}"
#             )
            
#             # ── Step 6: Broadcast via WebSocket to dashboard ─────────────────
#             try:
#                 ws_manager = get_ws_manager()
#                 await ws_manager.broadcast_alert(
#                     alert_id=alert_id,
#                     event_id=event_id,
#                     location_name=location_name,
#                     severity=severity,
#                     threat_score=confidence,
#                     classification="scream" if is_scream else "noise",
#                     transcript=f"Scream detected with {confidence:.1%} confidence",
#                     audio_url=f"/api/events/{event_id}/audio",
#                     timestamp=event_timestamp.isoformat()
#                 )
#                 logger.info(f"[Scream Detect] WebSocket broadcast sent for alert {alert_id}")
#             except Exception as e:
#                 logger.error(f"[Scream Detect] WebSocket broadcast failed: {e}")
        
#         # ── Step 7: Return response to ESP32 ─────────────────────────────────
#         return {
#             "status": "success",
#             "event_id": event_id,
#             "is_scream": is_scream,
#             "confidence": confidence,
#             "alert_fired": alert_fired,
#             "alert_id": alert_id,
#             "message": "Scream detected!" if is_scream else "No scream detected"
#         }
        
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"[Scream Detect] Unexpected error: {e}")
#         import traceback
#         traceback.print_exc()
#         raise HTTPException(500, f"Internal server error: {str(e)}")


# @router.get(
#     "/{event_id}/audio",
#     summary="Stream audio clip for dashboard playback",
# )
# async def stream_audio(
#     event_id: str,
#     db:       Session = Depends(get_db),
#     storage             = Depends(get_audio_capture),
# ):
#     """Dashboard calls this to play back the audio for an incident."""
#     event = db.query(Event).filter(Event.event_id == event_id).first()
#     if not event:
#         raise HTTPException(404, "Event not found")

#     clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
#     if not clip or not clip.file_path:
#         raise HTTPException(404, "Audio clip not found")

#     return FileResponse(
#         clip.file_path,
#         media_type = "audio/wav",
#         filename   = f"incident_{event_id}.wav",
#     )


# @router.get(
#     "/all",
#     summary="Get all events for the Scream Alerts dashboard tab",
# )
# async def get_all_events(
#     limit: int = 100,
#     db: Session = Depends(get_db),
# ):
#     """
#     Returns all events from the database, ordered by timestamp descending.
#     Used by the Scream Alerts tab in the dashboard.
#     """
#     try:
#         events = (
#             db.query(Event)
#             .order_by(desc(Event.timestamp))
#             .limit(limit)
#             .all()
#         )
        
#         result = []
#         for event in events:
#             device = db.query(Device).filter(Device.device_id == event.device_id).first()
#             location_name = "Unknown"
#             if device and device.location_id:
#                 location = db.query(Location).filter(Location.location_id == device.location_id).first()
#                 if location:
#                     location_name = location.location_name
            
#             result.append({
#                 "id": event.event_id,
#                 "device_id": event.device_id or "Unknown",
#                 "location_id": device.location_id if device else "Unknown",
#                 "location_name": location_name,
#                 "timestamp": event.timestamp.isoformat() if event.timestamp else None,
#                 "intensity": event.intensity or 0,
#                 "pitch": event.pitch or 0,
#                 "confidence_score": event.confidence_score or 0,
#             })
        
#         return {"events": result}
        
#     except Exception as e:
#         logger.error(f"Error fetching all events: {e}")
#         return {"events": []}


# @router.get(
#     "/stats",
#     summary="Get event statistics for the dashboard",
# )
# async def get_event_stats(
#     db: Session = Depends(get_db),
# ):
#     """
#     Returns statistics about events (total, high/medium/low confidence).
#     """
#     try:
#         total = db.query(Event).count()
        
#         high = db.query(Event).filter(Event.confidence_score > 0.7).count()
#         medium = db.query(Event).filter(
#             Event.confidence_score > 0.4,
#             Event.confidence_score <= 0.7
#         ).count()
#         low = db.query(Event).filter(Event.confidence_score <= 0.4).count()
#         screams = db.query(Event).filter(Event.confidence_score > 0.5).count()
        
#         return {
#             "total": total,
#             "high": high,
#             "medium": medium,
#             "low": low,
#             "screams": screams
#         }
        
#     except Exception as e:
#         logger.error(f"Error getting event stats: {e}")
#         return {
#             "total": 0,
#             "high": 0,
#             "medium": 0,
#             "low": 0,
#             "screams": 0
#         }



"""
api/events.py
═══════════════════════════════════════════════════════
UPDATED API CONTRACT for Supabase Storage Flow
═══════════════════════════════════════════════════════

POST /api/events/notify-upload
  ESP32 calls this immediately AFTER successfully streaming the WAV to Supabase.
  Sends: JSON body containing metadata and the Supabase file path/URL.

GET  /api/events/{event_id}/audio
  Dashboard calls this to stream the audio clip for playback directly from Supabase.

GET  /api/events/all
  Get all events for the Scream Alerts tab

GET  /api/events/stats
  Get event statistics for the dashboard
"""
import io
import os
import logging
import uuid
import io
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from supabase import create_client, Client

from models.database import Event, AudioClip, Device, Location, Alert
from api.dependencies import get_db, get_ws_manager
from services.scream_analyzer import ScreamAnalyzer
from models.database import Event, AudioClip
from models.schemas import ProcessingResponse
from api.dependencies import get_db, get_pipeline, get_audio_storage

router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
logger = logging.getLogger(__name__)

# ── Initialize Scream Analyzer (once at startup) ──────────────────────────
_analyzer = ScreamAnalyzer()

# ── Supabase Python Client Setup ─────────────────────────────────────────────
# Replace these with your actual environment variables or config keys
SUPABASE_URL = "https://lljkntrbthoycllpeckq.supabase.co"  
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxsamtudHJidGhveWNsbHBlY2txIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MjI2Mzg0MSwiZXhwIjoyMDk3ODM5ODQxfQ.-mOZpwV74iChOt8bOFHr56uATPl0htfJrGRI4-clmOw"  # 👈 Use Service Role Key to bypass RLS policies
BUCKET_NAME = "audio-alerts"

supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


@router.post(
    "/notify-upload",
    summary="[ESP32] Notify backend that audio has been uploaded to Supabase",
)
async def notify_supabase_upload(
    payload: dict = Body(
        ..., 
        example={
            "device_id": "esp32-001",
            "location_id": "loc-01",
            "timestamp": "2026-06-29T15:53:00Z",
            "sound_level": "3200",
            "duration_seconds": "8.0",
            "supabase_file_path": "esp32-001_20260629T155300Z.wav"
        }
    ),
    db: Session = Depends(get_db),
):
    """
    Entry point for the new Supabase decentralized recording pipeline.
    
    The ESP32 drops the file into Supabase Storage over raw TCP first, then sends 
    this lightweight JSON metadata payload to the cloud. The backend downloads the 
    asset, runs the ML scream analyzer, updates Postgres, and fires WebSockets.
    """
    try:
        device_id = payload.get("device_id")
        location_id = payload.get("location_id")
        timestamp = payload.get("timestamp")
        sound_level = payload.get("sound_level", "0")
        duration_seconds = payload.get("duration_seconds", "8.0")
        file_path = payload.get("supabase_file_path")

        if not file_path:
            raise HTTPException(400, "Missing 'supabase_file_path' in payload")

        logger.info(f"[Supabase Workflow] Processing event from {device_id} for file: {file_path}")

        # ── Step 1: Download audio bytes back from Supabase Storage ──────────
        try:
            audio_bytes = supabase_client.storage.from_(BUCKET_NAME).download(file_path)
            if not audio_bytes:
                raise ValueError("Downloaded file payload is empty")
        except Exception as e:
            logger.error(f"Failed to download file from Supabase Storage: {e}")
            raise HTTPException(500, f"Supabase retrieval error: {str(e)}")

        # ── Step 2: Analyze for scream using scream_analyzer.py ─────────────
        try:
            result = _analyzer.analyze(audio_bytes)
        except Exception as e:
            logger.error(f"Analyzer crashed: {e}")
            raise HTTPException(500, f"Analyzer pipeline exception: {str(e)}")

        if result.get('error'):
            logger.error(f"[Scream Detect] Analysis error: {result['error']}")
            raise HTTPException(500, f"Analysis failed: {result['error']}")
        
        is_scream = result.get('is_scream', False)
        confidence = result.get('confidence', 0.0)
        
        logger.info(f"[Scream Detect] Result: is_scream={is_scream}, confidence={confidence:.3f}")
        
        # ── Step 3: Save metadata to PostgreSQL database ────────────────────
        event_id = str(uuid.uuid4())
        
        try:
            event_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            event_timestamp = datetime.utcnow()
            logger.warning("[Scream Detect] Invalid timestamp formatting passed, falling back to UTC")
        
        # Ensure device presence 
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device:
            logger.info(f"[Scream Detect] Lazily creating device mapping registry: {device_id}")
            device = Device(device_id=device_id, location_id=location_id, status="online")
            db.add(device)
            db.commit()
            db.refresh(device)
        
        # Create core event payload instance
        try:
            sound_level_int = int(float(sound_level))
        except ValueError:
            sound_level_int = 0

        event = Event(
            event_id=event_id,
            device_id=device_id,
            timestamp=event_timestamp,
            intensity=float(sound_level_int),  
            pitch=0.0,      
            confidence_score=confidence  
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        
        # Track clip metadata matching the storage path key target
        clip_id = str(uuid.uuid4())
        try:
            duration = float(duration_seconds)
        except ValueError:
            duration = 8.0

        clip = AudioClip(
            clip_id=clip_id,
            event_id=event_id,
            file_path=file_path,  # Storing the absolute Supabase path lookup key string
            duration=duration
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)

        # ── Step 4: If scream detected, generate active Alert instance ──────
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
            
            logger.warning(f"[Scream Detect] 🚨 ALERT FIRED! severity={severity}, confidence={confidence:.3f}")
            
            # ── Step 5: Broadcast real-time update to web dashboards ────────
            try:
                ws_manager = get_ws_manager()
                await ws_manager.broadcast_alert(
                    alert_id=alert_id,
                    event_id=event_id,
                    location_name=location_name,
                    severity=severity,
                    threat_score=confidence,
                    classification="scream",
                    transcript=f"Scream detected with {confidence:.1%} confidence",
                    audio_url=f"/api/events/{event_id}/audio",
                    timestamp=event_timestamp.isoformat()
                )
            except Exception as e:
                logger.error(f"[Scream Detect] WebSocket transmission failed: {e}")
        
        return {
            "status": "success",
            "event_id": event_id,
            "is_scream": is_scream,
            "confidence": confidence,
            "alert_fired": alert_fired,
            "alert_id": alert_id,
            "message": "Scream processed successfully" if is_scream else "Background noise processed successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Supabase Notify Wrapper] Exception thrown: {e}")
        raise HTTPException(500, f"Internal routing processing failure: {str(e)}")


@router.get(
    "/{event_id}/audio",
    summary="Stream audio clip for dashboard playback",
)
async def stream_audio(
    event_id: str,
    db:       Session = Depends(get_db),
    storage             = Depends(get_audio_storage),
    
    #     db: Session = Depends(get_db),
    # ):
        
    #     Dashboard proxy media handler.
        
    #     Downloads the matching resource from your private Supabase Storage infrastructure 
    #     on-demand and pipes it straight down to the client media buffer elements securely.

):
    """Dashboard calls this to play back the audio for an incident.

    Serves the clip from Supabase Storage when it was uploaded there, otherwise
    streams the local file — transparent to the caller either way.
    """
    event = db.query(Event).filter(Event.event_id == event_id).first()
    if not event:
        raise HTTPException(404, "Target Event metadata identifier not found")

    clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
    if not clip or not clip.file_path:
        raise HTTPException(404, "Associated AudioClip record context missing")

    # Remote (Supabase) clip → fetch bytes and stream them through our endpoint.
    if storage.is_remote(clip.file_path):
        data = storage.get_bytes(clip.file_path)
        if data is None:
            raise HTTPException(404, "Audio clip not found")
        return StreamingResponse(
            io.BytesIO(data),
            media_type = "audio/wav",
            headers    = {"Content-Disposition": f'inline; filename="incident_{event_id}.wav"'},
        )

    # Local clip → serve straight off disk.
    if not os.path.exists(clip.file_path):
        raise HTTPException(404, "Audio clip not found")
    return FileResponse(
        clip.file_path,
        media_type = "audio/wav",
        filename   = f"incident_{event_id}.wav",
    )



#     try:
#         # Pull stream map from cloud pipeline dynamically instead of seeking local OS partitions
#         audio_bytes = supabase_client.storage.from_(BUCKET_NAME).download(clip.file_path)
#         return StreamingResponse(
#             io.BytesIO(audio_bytes),
#             media_type="audio/wav",
#             headers={"Content-Disposition": f"attachment; filename=incident_{event_id}.wav"}
#         )
#     except Exception as e:
#         logger.error(f"Failed pulling asset payload back for distribution: {e}")
#         raise HTTPException(500, f"Error gathering external tracking source content stream: {str(e)}")


# @router.get(
#     "/all",
#     summary="Get all events for the Scream Alerts dashboard tab",
# )
# async def get_all_events(
#     limit: int = 100,
#     db: Session = Depends(get_db),
# ):
#     """Returns all historic events from tracking database, ordered chronologically descending."""
#     try:
#         events = (
#             db.query(Event)
#             .order_by(desc(Event.timestamp))
#             .limit(limit)
#             .all()
#         )
        
#         result = []
#         for event in events:
#             device = db.query(Device).filter(Device.device_id == event.device_id).first()
#             location_name = "Unknown"
#             location_id = "Unknown"
            
#             if device and device.location_id:
#                 location_id = device.location_id
#                 location = db.query(Location).filter(Location.location_id == device.location_id).first()
#                 if location:
#                     location_name = location.location_name
            
#             result.append({
#                 "id": event.event_id,
#                 "device_id": event.device_id or "Unknown",
#                 "location_id": location_id,
#                 "location_name": location_name,
#                 "timestamp": event.timestamp.isoformat() if event.timestamp else None,
#                 "intensity": event.intensity or 0,
#                 "pitch": event.pitch or 0,
#                 "confidence_score": event.confidence_score or 0,
#             })
        
#         return {"events": result}
        
#     except Exception as e:
#         logger.error(f"Error fetching all events: {e}")
#         return {"events": []}


# @router.get(
#     "/stats",
#     summary="Get event statistics for the dashboard",
# )
# async def get_event_stats(
#     db: Session = Depends(get_db),
# ):
#     """Calculates status overview metric counters for dashboard view displays."""
#     try:
#         total = db.query(Event).count()
#         high = db.query(Event).filter(Event.confidence_score > 0.7).count()
#         medium = db.query(Event).filter(Event.confidence_score > 0.4, Event.confidence_score <= 0.7).count()
#         low = db.query(Event).filter(Event.confidence_score <= 0.4).count()
#         screams = db.query(Event).filter(Event.confidence_score > 0.5).count()
        
#         return {
#             "total": total,
#             "high": high,
#             "medium": medium,
#             "low": low,
#             "screams": screams
#         }
#     except Exception as e:
#         logger.error(f"Error getting event stats: {e}")
#         return {"total": 0, "high": 0, "medium": 0, "low": 0, "screams": 0}

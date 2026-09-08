"""
api/events.py
═══════════════════════════════════════════════════════
MERGED SUPABASE CONTRACT — Scream Analysis (You) + NLP Pipeline (Teammate)
═══════════════════════════════════════════════════════
"""
import asyncio
import logging
import uuid
import io
import os
import tempfile
from datetime import datetime
from fastapi import APIRouter, Body, UploadFile, File, Form, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from supabase import create_client, Client
from config.settings import get_settings

from models.database import Event, AudioClip, Device, Location, Alert, User
from models.schemas import ProcessingResponse
from api.dependencies import (
    get_db,
    get_pipeline,
    get_ws_manager,
    get_audio_storage,
    get_current_user,
    get_current_user_query_ok,
    verify_device_key,
    verify_device_key_form,
)
from api.devices import touch_device_heartbeat
from services.storage_service import AudioStorageService
from services.audio_capture_service import AudioCaptureService
from services.scream_analyzer import ScreamAnalyzer
from utils.rate_limit import limiter

METADATA_CACHE = {}

router = APIRouter(prefix="/api/events", tags=["Module 1+2 — Audio Ingestion"])
logger = logging.getLogger(__name__)

# Initialize your Scream Analyzer
_analyzer = ScreamAnalyzer()

@router.post(
    "/audio",
    summary="[ESP32] Notify backend after uploading audio to Supabase",
    # Device-aware key gate (FR25): reads device_id from the form so it can
    # enforce a per-device key when the device is enrolled, falling back to the
    # global DEVICE_API_KEY otherwise. Runs before body validation (401 first).
    dependencies=[Depends(verify_device_key_form)],   # devices use X-API-Key, not JWT
)
@limiter.limit("30/minute")   # abuse protection — per client IP
async def receive_audio_event(
    request: Request,
    device_id: str = Form(...),
    location_id: str = Form(...),
    timestamp: str = Form(...),
    sound_level: str = Form("0"),
    duration_seconds: str = Form("8"),
    # ⬇️ IMPORTANT: Add this field!
    supabase_file_path: str = Form(...),  # The filename uploaded to Supabase
    db: Session = Depends(get_db),
    audio_storage: AudioStorageService = Depends(get_audio_storage),
    pipeline = Depends(get_pipeline),
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
            logger.exception(f"[Audio] Failed to download from Supabase: {supabase_file_path}")
            # Try with "incidents/" prefix as fallback
            try:
                audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/incidents/{supabase_file_path}")
                if not audio_bytes:
                    raise ValueError("Downloaded file is empty")
                logger.info(f"[Audio] Downloaded {len(audio_bytes):,} bytes from incidents/{supabase_file_path}")
            except Exception as e2:
                logger.exception(f"[Audio] Both download attempts failed for {supabase_file_path}")
                return {
                    "status": "error",
                    "message": "Audio download failed"
                }
        
        # ── Step 2: Run scream analysis ──────────────────────────────────────────
        result = await asyncio.to_thread(_analyzer.analyze, audio_bytes)
        
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

        # FR29: this successful ingestion is also a liveness signal.
        touch_device_heartbeat(db, device_id)

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
        
        # ── Step 5: Run STT + NLP pipeline, apply alert rule, broadcast ──────────
        pipeline_result = await pipeline.process_stored_audio(
            db=db,
            audio_bytes=audio_bytes,
            event=event,
            clip=clip,
            location_id=location_id,
            scream_confidence=confidence,
            is_scream=is_scream,
        )

        # ── Step 6: Return response to ESP32 ──────────────────────────────────────
        return {
            "status": "success",
            "event_id": event_id,
            "is_scream": is_scream,
            "confidence": confidence,
            "alert_fired": pipeline_result.get("alert_fired", False),
            "alert_id": pipeline_result.get("alert_id"),
            "transcript": pipeline_result.get("transcript"),
            "threat_score": pipeline_result.get("threat_score"),
            "severity": pipeline_result.get("severity"),
            "classification": pipeline_result.get("classification"),
            "message": "Alert fired!" if pipeline_result.get("alert_fired") else "Processed — no alert"
        }
        
    except Exception as e:
        logger.exception("[Audio] Error processing audio event")
        raise HTTPException(500, "Processing failed")


@router.get("/{event_id}/audio", summary="Stream audio clip straight out of Supabase Storage")
async def stream_audio(
    event_id: str,
    db: Session = Depends(get_db),
    audio_storage: AudioStorageService = Depends(get_audio_storage),
    # The dashboard <audio> tag can't send headers, so this dependency also
    # accepts ?token=<jwt> as a fallback (Authorization header wins if both set).
    user: User = Depends(get_current_user_query_ok),
):
    """Pipes the file from Supabase right down to the dashboard browser player."""
    clip = db.query(AudioClip).filter(AudioClip.event_id == event_id).first()
    if not clip or not clip.file_path:
        raise HTTPException(404, "Audio file reference context missing")

    file_path = clip.file_path

    signed_url = audio_storage.get_signed_url(file_path)
    if signed_url:
        # Browser follows redirect to Supabase CDN – audio plays instantly
        return RedirectResponse(url=signed_url, status_code=302)

    # ── Resolve to a plain object key ─────────────────────────────────────
    
    # audio_bytes = None

    audio_bytes = audio_storage.get_bytes(file_path)

    if not audio_bytes:
        raise HTTPException(404, "Audio file not found")
    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"inline; filename={event_id}.wav",
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(audio_bytes)),
            "Cache-Control": "no-cache",
        },
    )

    # ── Stream with correct headers for browser audio playback ────────────
    # Content-Disposition: inline  → browser plays it, not downloads it
    # Accept-Ranges: bytes         → browser can seek and resume
    # Content-Length               → browser shows correct duration bar
    # audio_length = len(audio_bytes)

    # if not audio_bytes and file_path.endswith(".wav"):
    #     # Format: "incidents/<uuid>.wav" or plain "<uuid>.wav"
    #     audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{file_path}")

    # if not audio_bytes and file_path.endswith(".wav"):
    #     # Last resort: pass as-is to get_bytes (handles local files too)
    #     audio_bytes = audio_storage.get_bytes(file_path)

    # if not audio_bytes:
    #     logger.error(f"Audio not found for event {event_id}, file_path={file_path}")
    #     raise HTTPException(404, "Audio file not found")

    

@router.get(
    "/all",
    summary="Get all events for the Scream Alerts dashboard tab",
)
async def get_all_events(
    limit: int = Query(100, ge=1, le=500),
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
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

            confidence = event.confidence_score or 0
            # is_scream = confidence >= 0.70  # Match backend threshold
            is_scream = confidence >= 0.60  # Match backend threshold

            audio_url = None
            if event.audio_clip:
                # If file_path is stored, construct the URL
                file_path = event.audio_clip.file_path
                if file_path:
                    # If it's a Supabase path, use the storage URL
                    if file_path.startswith('supabase://'):
                        audio_url = f"/api/events/{event.event_id}/audio"
                    else:
                        audio_url = f"/api/events/{event.event_id}/audio"

            result.append({
                "id": event.event_id,
                "device_id": event.device_id or "Unknown",
                "location_id": location_id,
                "location_name": location_name,
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "intensity": event.intensity or 0,
                "pitch": event.pitch or 0,
                "confidence_score": event.confidence_score or 0,
                "is_scream": is_scream,  
                "audio_url": audio_url,
                "duration": event.audio_clip.duration if event.audio_clip else None,  
            })
        
        return {"events": result}
        
    except Exception as e:
        logger.exception("Error fetching all events")
        return {"events": []}


@router.get(
    "/stats",
    summary="Get event statistics for the dashboard",
)
async def get_event_stats(
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
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
        logger.exception("Error getting event stats")
        return {"total": 0, "high": 0, "medium": 0, "low": 0, "screams": 0}



# Add to events.py - Webhook endpoint for Supabase Storage events

@router.post(
    "/webhook/supabase-storage",
    summary="[Webhook] Triggered by Supabase when new audio is uploaded",
    dependencies=[Depends(verify_device_key)],   # machine-to-machine — X-API-Key, not JWT
)
@limiter.limit("30/minute")   # abuse protection — per client IP
async def supabase_storage_webhook(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    audio_storage: AudioStorageService = Depends(get_audio_storage),
    pipeline = Depends(get_pipeline),
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
        
        # ── Handle metadata JSON file ──────────────────────────────────────
        if file_name.endswith('.json'):
            logger.info(f"[Webhook] Received metadata file: {file_name}")
            # Download and parse metadata
            try:
                json_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{file_name}")
                if json_bytes:
                    import json
                    metadata = json.loads(json_bytes)
                    # Store metadata in a dictionary for later use
                    # We'll associate it with the .wav file when it arrives
                    uuid_base = file_name.replace('.json', '')
                    # Store in a temporary cache or database
                    METADATA_CACHE[uuid_base] = metadata  
                    # logger.info(f"[Webhook] Metadata: {metadata}")

                    # ⭐ UPDATE THE EXISTING EVENT!
                    # Find the event by the WAV filename
                    wav_file_name = f"{uuid_base}.wav"
                    clip = db.query(AudioClip).filter(AudioClip.file_path == wav_file_name).first()
                    
                    if clip:
                        event = db.query(Event).filter(Event.event_id == clip.event_id).first()
                        if event:
                            # Update with correct values
                            event.intensity = float(metadata.get("sound_level", 0))
                            
                            # Parse timestamp
                            timestamp_str = metadata.get("timestamp", "")
                            if timestamp_str:
                                try:
                                    event_timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                                    if event_timestamp.tzinfo is not None:
                                        event_timestamp = event_timestamp.astimezone(datetime.timezone.utc)
                                    event.timestamp = event_timestamp
                                    logger.info(f"[Webhook] Updated Event {event.event_id} with timestamp {event_timestamp} and intensity {event.intensity}")
                                except Exception as e:
                                    logger.warning(f"[Webhook] Failed to parse timestamp: {e}")
                            
                            db.commit()
                            logger.info(f"[Webhook] ✅ Updated Event with metadata from JSON")


                    # For simplicity, we'll return early since .wav will trigger separately
                    return {
                        "status": "success",
                        "message": "Metadata cached and Event updated ",
                        "metadata": metadata
                    }
            except Exception as e:
                logger.error(f"[Webhook] Failed to process metadata: {e}")
                return {"status": "error", "message": str(e)}

        # ── Check if this is an audio file ───────────────────────────────────
        if not file_name.endswith('.wav'):
            logger.info(f"[Webhook] Skipping non-audio file: {file_name}")
            return {"status": "skipped", "reason": "Not a WAV file"}

        # ── Idempotency guard: skip files already processed ──────────────────
        # AudioClip.file_path is stored as the bare Supabase object name below,
        # so an exact match on file_name means this webhook already ran for it.
        existing_clip = db.query(AudioClip).filter(AudioClip.file_path == file_name).first()
        if existing_clip:
            logger.info(f"[Webhook] File already processed, skipping: {file_name}")
            return {"status": "skipped", "message": "File already processed"}

        # ── Extract UUID from filename ──────────────────────────────────────
        uuid_base = file_name.replace('.wav', '')
        
        # Define default fallbacks 
        device_id = "esp32-001"
        location_id = "loc-toilet-a"
        sound_level = 0
        timestamp_str = ""

        metadata = METADATA_CACHE.pop(uuid_base, None)  # ✅ Retrieve and remove

        # ── Try to get metadata from JSON file ──────────────────────────────
        if metadata:
            device_id = metadata.get("device_id", device_id)
            location_id = metadata.get("location_id", location_id)
            sound_level = float(metadata.get("sound_level", 0))
            timestamp_str = metadata.get("timestamp", "")
            logger.info(f"[Webhook] Using cached metadata for {file_name}: {metadata}")
        else:
            # Try to fetch the JSON metadata file
            json_file_name = f"{uuid_base}.json"
            try:
                json_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{json_file_name}")
                if json_bytes:
                    import json
                    metadata = json.loads(json_bytes)
                    device_id = metadata.get("device_id", device_id)
                    location_id = metadata.get("location_id", location_id)
                    sound_level = float(metadata.get("sound_level", 0))
                    timestamp_str = metadata.get("timestamp", "")
                    logger.info(f"[Webhook] Found metadata for {file_name}: {metadata}")
                else:
                    logger.warning(f"[Webhook] No metadata file found for {file_name}")
            except Exception as e:
                logger.warning(f"[Webhook] Could not load metadata: {e}")


        # # ── Extract device info from filename ────────────────────────────────
        # # Filename format: esp32-001_20260629T155300Z.wav
        # # OR: 1ea33c87-35db-40a3-903e-d1e512fe5c4a.wav (UUID)
        # device_id = "esp32-001"  # Default fallback
        # location_id = "loc-toilet-a"

        # # Try to extract device ID from filename if using old format
        # if file_name.startswith("esp32-"):
        #     parts = file_name.split('_')
        #     if len(parts) >= 1:
        #         device_id = parts[0]
        
        # ── Download audio from Supabase ──────────────────────────────────────
        try:
            audio_bytes = audio_storage.get_bytes(f"supabase://audio-clips/{file_name}")
            if not audio_bytes:
                raise ValueError("Downloaded file is empty")
            logger.info(f"[Webhook] Downloaded {len(audio_bytes):,} bytes from {file_name}")
        except Exception as e:
            logger.exception(f"[Webhook] Failed to download from Supabase: {file_name}")
            raise HTTPException(500, "Audio download failed")
        
        # ── Run scream analysis ──────────────────────────────────────────────
        result = await asyncio.to_thread(_analyzer.analyze, audio_bytes)
        
        if result.get('error'):
            logger.error(f"[Webhook] Analysis error: {result['error']}")
            return {"status": "error", "message": result['error']}
        
        is_scream = result.get('is_scream', False)
        confidence = result.get('confidence', 0.0)
        
        logger.info(f"[Webhook] Analysis result: is_scream={is_scream}, confidence={confidence:.3f}")
        
        # # ── Extract timestamp from filename OR use current time ──────────────
        # # If filename is UUID, we need to get timestamp from file metadata
        # # For now, use current time
        # event_timestamp = datetime.utcnow()
        # ── Parse timestamp ──────────────────────────────────────────────────
        event_timestamp = datetime.utcnow()
        if timestamp_str:
            try:
                event_timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                if event_timestamp.tzinfo is not None:
                    event_timestamp = event_timestamp.astimezone(datetime.timezone.utc)
                logger.info(f"[Webhook] Parsed timestamp: {timestamp_str} → UTC: {event_timestamp}")
            except Exception as e:
                logger.warning(f"[Webhook] Failed to parse timestamp '{timestamp_str}': {e}")
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
            # timestamp=timestamp,
            # intensity=0.0,  # Not calculated on ESP
            intensity=float(sound_level) if sound_level else 0.0,  
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
            duration=8.0
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)
        
        # ── (OLD) If scream detected, create alert and broadcast ─────────────
        # alert_fired = False
        # alert_id = None
        #
        # if is_scream:
        #     severity = "high" if confidence > 0.7 else ("medium" if confidence > 0.4 else "low")
        #
        #     alert_id = str(uuid.uuid4())
        #     alert = Alert(
        #         alert_id=alert_id,
        #         event_id=event_id,
        #         severity=severity,
        #         status="active",
        #         created_at=datetime.utcnow()
        #     )
        #     db.add(alert)
        #     db.commit()
        #     db.refresh(alert)
        #     alert_fired = True
        #
        #     location = db.query(Location).filter(Location.location_id == location_id).first()
        #     location_name = location.location_name if location else location_id
        #
        #     logger.warning(f"[Webhook] 🚨 ALERT FIRED! severity={severity}, confidence={confidence:.3f}")
        #
        #     # Broadcast via WebSocket
        #     try:
        #         ws_manager = get_ws_manager()
        #         await ws_manager.broadcast_alert(
        #             alert_id=alert_id,
        #             event_id=event_id,
        #             location_name=location_name,
        #             severity=severity,
        #             threat_score=confidence,
        #             classification="scream" if is_scream else "noise",
        #             transcript=f"Scream detected with {confidence:.1%} confidence",
        #             audio_url=f"/api/events/{event_id}/audio",
        #             timestamp=event_timestamp.isoformat()
        #         )
        #         logger.info(f"[Webhook] WebSocket broadcast sent")
        #     except Exception as e:
        #         logger.error(f"[Webhook] WebSocket broadcast failed: {e}")
        #
        # return {
        #     "status": "success",
        #     "event_id": event_id,
        #     "is_scream": is_scream,
        #     "confidence": confidence,
        #     "alert_fired": alert_fired,
        #     "alert_id": alert_id,
        #     "message": "Scream detected!" if is_scream else "No scream detected"
        # }

        # ── Run STT + NLP pipeline, apply alert rule, broadcast ──────────────
        pipeline_result = await pipeline.process_stored_audio(
            db=db,
            audio_bytes=audio_bytes,
            event=event,
            clip=clip,
            location_id=location_id,
            scream_confidence=confidence,
            is_scream=is_scream,
        )

        return {
            "status": "success",
            "event_id": event_id,
            "is_scream": is_scream,
            "confidence": confidence,
            "alert_fired": pipeline_result.get("alert_fired", False),
            "alert_id": pipeline_result.get("alert_id"),
            "transcript": pipeline_result.get("transcript"),
            "threat_score": pipeline_result.get("threat_score"),
            "severity": pipeline_result.get("severity"),
            "classification": pipeline_result.get("classification"),
            "message": "Alert fired!" if pipeline_result.get("alert_fired") else "Processed — no alert"
        }
        
    except Exception as e:
        logger.exception("[Webhook] Error processing storage webhook")
        raise HTTPException(500, "Processing failed")

"""
services/processing_pipeline.py
═══════════════════════════════════════════════════════
ORCHESTRATOR: Connects all 4 of your modules together
═══════════════════════════════════════════════════════

Exact flow matching Figure 4.1 System Architecture Diagram:

  [Lee's ESP32 sends HTTP POST]
       │
       ▼
  ┌─ MODULE 1: Speech Detection & Audio Capture ─────────┐
  │  1. Validate audio                                    │
  │  2. Voice Activity Detection (VAD)                    │
  │  3. Preprocess → 16kHz mono WAV                       │
  │  4. Save to Audio Object Storage                      │
  └──────────────────────────────────────────────────────┘
       │ file_path + speech_detected
       ▼
  ┌─ MODULE 2: Cloud Processing & AI Analysis ───────────┐
  │  5. Whisper STT → transcript text                     │
  │  6. XLM-RoBERTa NLP → threat_score + classification  │
  │  7. Save Event, AudioClip, Transcript, Analysis to DB │
  └──────────────────────────────────────────────────────┘
       │ threat_score >= threshold?
       ▼
  ┌─ Save Alert to DB ───────────────────────────────────┐
  │  8. Create Alert record (active)                      │
  │  9. WebSocket broadcast → Dashboard (Module 4)        │
  └──────────────────────────────────────────────────────┘
       │
       ▼
  ┌─ MODULE 3 & 4: Reporting + Dashboard ────────────────┐
  │  All data is now queryable via REST API               │
  │  Dashboard polls /api/alerts and /api/analytics       │
  └──────────────────────────────────────────────────────┘
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from models.database import Event, AudioClip, Transcript, Analysis, Alert, Device, Location
from models.schemas import ProcessingResponse, TranscriptResult, AnalysisResult
from services.audio_capture_service import AudioCaptureService
from services.stt_service import STTService
from services.nlp_service import NLPService

logger = logging.getLogger(__name__)


class ProcessingPipeline:

    def __init__(
        self,
        audio_capture: AudioCaptureService,
        stt:           STTService,
        nlp:           NLPService,
        websocket_mgr,                      # WebSocketManager injected at runtime
        threshold:     float = 0.75,
    ):
        self.audio_capture = audio_capture
        self.stt           = stt
        self.nlp           = nlp
        self.ws            = websocket_mgr
        self.threshold     = threshold

    async def process(
        self,
        db:              Session,
        audio_bytes:     bytes,
        filename:        str,
        device_id:       str,
        location_id:     str,
        timestamp_str:   str,
        intensity:       float,
        pitch:           float,
        edge_confidence: float,
        duration_hint:   float,
    ) -> ProcessingResponse:

        # ── Ensure device exists in DB ────────────────────────────────────────
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device:
            logger.warning(f"Unknown device '{device_id}' — auto-registering.")
            device = Device(device_id=device_id, location_id=location_id, status="online")
            db.add(device)
            db.commit()

        event_id = str(uuid.uuid4())

        # ── MODULE 1: Speech Detection & Audio Capture ────────────────────────
        try:
            file_path, duration, speech_detected = self.audio_capture.receive_and_prepare(
                audio_bytes, filename, event_id
            )
        except ValueError as e:
            raise ValueError(f"Audio capture failed: {e}")

        # Create Event record
        event = Event(
            event_id         = event_id,
            device_id        = device_id,
            timestamp        = datetime.fromisoformat(timestamp_str),
            intensity        = intensity,
            pitch            = pitch,
            confidence_score = edge_confidence,
        )
        db.add(event)
        db.commit()
        db.refresh(event)

        # Create AudioClip record (Audio Object Storage path)
        clip = AudioClip(
            clip_id   = str(uuid.uuid4()),
            event_id  = event.event_id,
            file_path = file_path,
            duration  = duration,
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)

        # If VAD found no speech → discard, no further processing
        if not speech_detected:
            logger.info(f"Event {event_id}: VAD found no speech — discarded.")
            return ProcessingResponse(
                event_id    = event_id,
                clip_id     = clip.clip_id,
                transcript  = TranscriptResult(transcript_id="", text="[no speech detected]"),
                analysis    = AnalysisResult(
                    analysis_id    = "",
                    severity_level = "low",
                    classification = "normal",
                    threat_score   = 0.0,
                ),
                alert_fired = False,
                message     = "Discarded — no speech detected in audio clip",
            )

        # ── MODULE 2: Cloud Processing & AI Analysis ──────────────────────────

        # Part A: STT
        stt_result      = await self.stt.transcribe(file_path)
        transcript_text = stt_result["text"]
        language        = stt_result["language"]

        transcript = Transcript(
            transcript_id = str(uuid.uuid4()),
            clip_id       = clip.clip_id,
            text          = transcript_text,
        )
        db.add(transcript)
        db.commit()
        db.refresh(transcript)

        # Part B: NLP threat analysis
        threat = await self.nlp.analyse(transcript_text, language)

        analysis = Analysis(
            analysis_id    = str(uuid.uuid4()),
            transcript_id  = transcript.transcript_id,
            severity_level = threat.severity_level,
            classification = threat.classification,
            threat_score   = threat.threat_score,
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)

        # ── Alert: fire if score exceeds threshold ────────────────────────────
        alert_fired = False

        if threat.threat_score >= self.threshold:
            alert = Alert(
                alert_id   = str(uuid.uuid4()),
                event_id   = event.event_id,
                severity   = threat.severity_level,
                status     = "active",
                created_at = datetime.utcnow(),
            )
            db.add(alert)
            db.commit()
            db.refresh(alert)
            alert_fired = True

            # Fetch location name for WebSocket push
            location      = db.query(Location).filter(Location.location_id == location_id).first()
            location_name = location.location_name if location else location_id

            # ── MODULE 4: WebSocket push to dashboard ─────────────────────────
            await self.ws.broadcast_alert(
                alert_id       = alert.alert_id,
                event_id       = event.event_id,
                location_name  = location_name,
                severity       = threat.severity_level,
                threat_score   = threat.threat_score,
                classification = threat.classification,
                transcript     = transcript_text,
                audio_url      = f"/api/events/{event.event_id}/audio",
                timestamp      = event.timestamp.isoformat(),
            )

            logger.warning(
                f"ALERT FIRED | severity={threat.severity_level} | "
                f"score={threat.threat_score} | location={location_name} | "
                f"transcript='{transcript_text[:60]}'"
            )
        else:
            logger.info(
                f"Event {event_id}: score={threat.threat_score:.3f} "
                f"below threshold {self.threshold} — logged, no alert."
            )

        return ProcessingResponse(
            event_id    = event.event_id,
            clip_id     = clip.clip_id,
            transcript  = TranscriptResult(
                transcript_id = transcript.transcript_id,
                text          = transcript_text,
            ),
            analysis    = AnalysisResult(
                analysis_id    = analysis.analysis_id,
                severity_level = threat.severity_level,
                classification = threat.classification,
                threat_score   = threat.threat_score,
            ),
            alert_fired = alert_fired,
            message     = (
                f"Alert fired — {threat.severity_level} severity"
                if alert_fired else
                "Processed — below threat threshold"
            ),
        )

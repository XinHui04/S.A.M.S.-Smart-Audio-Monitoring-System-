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
import os
import tempfile
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from models.database import (
    Event, AudioClip, Transcript, Analysis, Alert, Device, Location, EmotionAnalysis,
)
from models.schemas import ProcessingResponse, TranscriptResult, AnalysisResult
from services.audio_capture_service import AudioCaptureService
from services.stt_service import STTService
from services.nlp_service import NLPService
from utils.nearest_staff import compute_nearest_staff

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _max_severity(a: str, b: str) -> str:
    """Worse of two severity labels; unknown labels rank above 'high' (fail loud, never understate)."""
    return a if _SEVERITY_RANK.get(a, 3) >= _SEVERITY_RANK.get(b, 3) else b


class ProcessingPipeline:

    def __init__(
        self,
        audio_capture: AudioCaptureService,
        stt:           STTService,
        nlp:           NLPService,
        websocket_mgr,                      # WebSocketManager injected at runtime
        mqtt          = None,               # MqttService injected at runtime (optional)
        threshold:     float = 0.75,
        audio_storage = None,               # AudioStorageService injected at runtime
        delete_local_after_upload: bool = True,
        ser           = None,               # SERService (§2.1.3) — None disables SER
        ser_boost:          float = 0.15,   # threat-score boost on negative emotion
        ser_min_confidence: float = 0.60,   # min SER confidence to apply the boost
        push          = None,               # PushService (FR9/FR12) — None disables Web Push
    ):
        self.audio_capture = audio_capture
        self.stt           = stt
        self.nlp           = nlp
        self.ws            = websocket_mgr
        self.mqtt          = mqtt
        self.threshold     = threshold
        self.audio_storage = audio_storage
        self.delete_local_after_upload = delete_local_after_upload
        self.ser                = ser
        self.ser_boost          = ser_boost
        self.ser_min_confidence = ser_min_confidence
        self.push               = push

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

        # Persist the durable copy to Audio Object Storage (Supabase Storage when
        # configured, else local disk). The local working file is retained for STT.
        if self.audio_storage is not None:
            file_ref = self.audio_storage.persist(file_path, event_id)
        else:
            file_ref = file_path

        # ── Atomic write section: Event, AudioClip, Transcript, Analysis and
        # Alert land in ONE transaction — intermediate flush() keeps IDs and
        # relationships usable, the single commit() at the end makes the run
        # all-or-nothing, and any failure rolls the whole run back.
        alert_fired = False
        alert       = None

        try:
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
            db.flush()

            # Create AudioClip record (Audio Object Storage path)
            clip = AudioClip(
                clip_id   = str(uuid.uuid4()),
                event_id  = event.event_id,
                file_path = file_ref,
                duration  = duration,
            )
            db.add(clip)
            db.flush()

            # If VAD found no speech → discard, no further processing
            # (Event + AudioClip still persist so the ingestion is auditable.)
            if not speech_detected:
                db.commit()
                logger.info(f"Event {event_id}: VAD found no speech — discarded.")
                self._cleanup_local_working_copy(file_ref, file_path)
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
                    alert_fired       = False,
                    message           = "Discarded — no speech detected in audio clip",
                    scream_confidence = edge_confidence,
                )

            # ── MODULE 2: Cloud Processing & AI Analysis ──────────────────────

            # Part A: STT
            stt_result      = await self.stt.transcribe(file_path)
            self._cleanup_local_working_copy(file_ref, file_path)
            transcript_text = stt_result["text"]
            language        = stt_result["language"]
            stt_confidence  = stt_result.get("confidence")

            transcript = Transcript(
                transcript_id = str(uuid.uuid4()),
                clip_id       = clip.clip_id,
                text          = transcript_text,
            )
            db.add(transcript)
            db.flush()

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
            db.flush()

            # ── Alert: fire if score exceeds threshold ────────────────────────
            if threat.threat_score >= self.threshold:
                alert = Alert(
                    alert_id   = str(uuid.uuid4()),
                    event_id   = event.event_id,
                    severity   = threat.severity_level,
                    status     = "active",
                    # created_at = datetime.utcnow(),
                    created_at = event.timestamp, 
                )
                db.add(alert)
                db.flush()
                alert_fired = True

            db.commit()
        except Exception:
            db.rollback()
            raise

        # ── Broadcasts run strictly AFTER the commit: an alert must never be
        # pushed to the dashboard and then rolled back.
        if alert_fired:
            # Fetch location name for WebSocket push
            location      = db.query(Location).filter(Location.location_id == location_id).first()
            location_name = location.location_name if location else location_id

            # FR30: display-side nearest-staff hint. Never let a ranking error
            # block the alert broadcast — log and send None (routing unchanged).
            nearest_staff = self._safe_nearest_staff(db, location_id)

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
                location_id    = location_id,   # FR16: route to assigned staff
                stt_confidence    = stt_confidence,
                nlp_confidence    = threat.model_confidence,
                scream_confidence = edge_confidence,
                nearest_staff     = nearest_staff,   # FR30 display-side hint
            )

            # ── MODULE 4: MQTT fan-out to external subscribers (Figs 4.1/4.2) ──
            if self.mqtt:
                self.mqtt.publish_alert(
                    alert_id       = alert.alert_id,
                    event_id       = event.event_id,
                    location_id    = location_id,
                    location_name  = location_name,
                    severity       = threat.severity_level,
                    threat_score   = threat.threat_score,
                    classification = threat.classification,
                    transcript     = transcript_text,
                    audio_url      = f"/api/events/{event.event_id}/audio",
                    timestamp      = event.timestamp.isoformat(),
                    stt_confidence    = stt_confidence,
                    nlp_confidence    = threat.model_confidence,
                    scream_confidence = edge_confidence,
                )

            # ── FR9/FR12: Web Push fan-out (fire-and-forget, never blocks) ────
            if self.push is not None:
                self.push.fire_and_forget(
                    alert_id      = alert.alert_id,
                    event_id      = event.event_id,
                    location_id   = location_id,   # FR16: route to assigned staff
                    location_name = location_name,
                    severity      = threat.severity_level,
                    timestamp     = event.timestamp.isoformat(),
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
                transcript_id  = transcript.transcript_id,
                text           = transcript_text,
                stt_confidence = stt_confidence,
            ),
            analysis    = AnalysisResult(
                analysis_id    = analysis.analysis_id,
                severity_level = threat.severity_level,
                classification = threat.classification,
                threat_score   = threat.threat_score,
                nlp_confidence = threat.model_confidence,
            ),
            alert_fired = alert_fired,
            message     = (
                f"Alert fired — {threat.severity_level} severity"
                if alert_fired else
                "Processed — below threat threshold"
            ),
            scream_confidence = edge_confidence,
        )

    async def process_stored_audio(
        self,
        db:                Session,
        audio_bytes:       bytes,
        event,                              # Event ORM row, already committed
        clip,                               # AudioClip ORM row, already committed
        location_id:       str,
        scream_confidence: float,
        is_scream:         bool,
    ) -> dict:
        """
        MODULE 2 for pre-stored audio: the ingestion endpoint has already
        downloaded the clip from Audio Object Storage and created the Event
        and AudioClip rows, so this method only runs STT → NLP → alert.

        Graceful degradation: if STT or NLP fails (e.g. missing API key),
        a placeholder Transcript + zero-score Analysis are saved and the
        scream detection alone still decides the alert — an AI failure
        must never suppress a scream alert.
        """
        # ── Part A: STT + SER (via a local temp WAV, always cleaned up) ───────
        transcript_text = ""
        language        = "unknown"
        stt_ok          = False
        stt_confidence  = None
        ser_result      = None

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        try:
            tmp.write(audio_bytes)
            tmp.close()
            try:
                stt_result      = await self.stt.transcribe(tmp.name)
                transcript_text = stt_result["text"]
                language        = stt_result["language"]
                stt_confidence  = stt_result.get("confidence")
                stt_ok          = True
            except Exception as e:
                logger.warning(f"Event {event.event_id}: STT failed ({e}) — continuing without transcript.")

            # ── Part A2: Speech Emotion Recognition (§2.1.3) ──────────────────
            # Runs after scream detection + STT on the SAME temp WAV, before it
            # is deleted. analyse() returns None on any failure — SER must
            # never break the pipeline.
            if self.ser is not None:
                ser_result = await self.ser.analyse(tmp.name)
        finally:
            try:
                if not tmp.closed:
                    tmp.close()
                os.remove(tmp.name)
            except OSError as e:
                logger.warning(f"Could not remove temp working copy: {e}")

        has_speech = stt_ok and bool(transcript_text.strip())
        stored_text = (
            transcript_text if has_speech
            else ("[no speech detected]" if stt_ok else "[transcription unavailable]")
        )

        # ── Atomic write section: Transcript, EmotionAnalysis, Analysis and
        # Alert land in ONE transaction — intermediate flush() keeps IDs and
        # relationships usable, the single commit() at the end makes the run
        # all-or-nothing, and any failure rolls the whole run back. (The Event
        # and AudioClip rows were committed by the caller and are unaffected.)
        try:
            transcript = Transcript(
                transcript_id = str(uuid.uuid4()),
                clip_id       = clip.clip_id,
                text          = stored_text,
            )
            db.add(transcript)
            db.flush()

            # ── Part B: NLP threat analysis (zero-score fallback on failure) ──
            # Severity fallback derived from the edge scream confidence
            if scream_confidence > 0.7:
                scream_severity = "high"
            elif scream_confidence > 0.4:
                scream_severity = "medium"
            else:
                scream_severity = "low"

            threat = None
            if has_speech:
                try:
                    threat = await self.nlp.analyse(transcript_text, language)
                except Exception as e:
                    logger.warning(f"Event {event.event_id}: NLP failed ({e}) — falling back to scream-only scoring.")

            if threat is not None:
                threat_score   = threat.threat_score
                severity       = threat.severity_level
                classification = threat.classification
            else:
                # No usable transcript (or NLP failure) — score on scream alone
                threat_score   = 0.0
                severity       = scream_severity
                classification = "scream" if is_scream else "unknown"

            nlp_confidence = threat.model_confidence if threat is not None else None

            # ── Part C: SER boost + persistence (§2.1.3) ──────────────────────
            # A confidently negative vocal tone corroborates the threat: boost
            # the NLP threat score BEFORE the alert-threshold comparison. The
            # emotion row is persisted for EVERY SER result (analytics value).
            emotion            = None
            emotion_confidence = None
            if ser_result is not None:
                emotion            = ser_result["emotion"]
                emotion_confidence = ser_result["confidence"]

                emotion_row = EmotionAnalysis(
                    event_id   = event.event_id,
                    emotion    = emotion,
                    confidence = emotion_confidence,
                )
                db.add(emotion_row)
                db.flush()

                if (
                    emotion in ("angry", "fearful")
                    and emotion_confidence >= self.ser_min_confidence
                ):
                    boosted = min(1.0, threat_score + self.ser_boost)
                    logger.info(
                        f"Event {event.event_id}: SER boost applied — "
                        f"emotion={emotion} ({emotion_confidence:.3f}) raises "
                        f"threat score {threat_score:.3f} → {boosted:.3f} "
                        f"(+{self.ser_boost})"
                    )
                    threat_score = boosted

            # ── Reconcile scream detection with NLP verdict ───────────────────
            # A detected scream may only RAISE severity, never lower it, and the
            # persisted/broadcast score must be one consistent value: the blend
            # of the (boosted) NLP score and the edge scream confidence.
            if is_scream:
                severity    = _max_severity(severity, scream_severity)
                final_score = max(threat_score, scream_confidence)
            else:
                final_score = threat_score

            analysis = Analysis(
                analysis_id    = str(uuid.uuid4()),
                transcript_id  = transcript.transcript_id,
                severity_level = severity,
                classification = classification,
                threat_score   = final_score,
            )
            db.add(analysis)
            db.flush()

            # ── Alert: fire on scream detection OR NLP threat score ──────────
            alert_fired = is_scream or threat_score >= self.threshold
            alert_id    = None

            if alert_fired:
                alert = Alert(
                    alert_id   = str(uuid.uuid4()),
                    event_id   = event.event_id,
                    severity   = severity,
                    status     = "active",
                    # created_at = datetime.utcnow(),
                    created_at = event.timestamp, 
                )
                db.add(alert)
                db.flush()
                alert_id = alert.alert_id

            db.commit()
        except Exception:
            db.rollback()
            raise

        # ── Broadcasts run strictly AFTER the commit: an alert must never be
        # pushed to the dashboard and then rolled back.
        if alert_fired:
            # Fetch location name for WebSocket push
            location      = db.query(Location).filter(Location.location_id == location_id).first()
            location_name = location.location_name if location else location_id

            # FR30: display-side nearest-staff hint. Never let a ranking error
            # block the alert broadcast — log and send None (routing unchanged).
            nearest_staff = self._safe_nearest_staff(db, location_id)

            # ── MODULE 4: WebSocket push to dashboard ─────────────────────────
            await self.ws.broadcast_alert(
                alert_id       = alert.alert_id,
                event_id       = event.event_id,
                location_name  = location_name,
                severity       = severity,
                threat_score   = final_score,
                classification = classification,
                transcript     = stored_text,
                audio_url      = f"/api/events/{event.event_id}/audio",
                timestamp      = event.timestamp.isoformat(),
                location_id    = location_id,   # FR16: route to assigned staff
                emotion            = emotion,
                emotion_confidence = emotion_confidence,
                stt_confidence     = stt_confidence,
                nlp_confidence     = nlp_confidence,
                scream_confidence  = scream_confidence,
                nearest_staff      = nearest_staff,   # FR30 display-side hint
            )

            # ── MODULE 4: MQTT fan-out to external subscribers (Figs 4.1/4.2) ──
            if self.mqtt:
                self.mqtt.publish_alert(
                    alert_id       = alert.alert_id,
                    event_id       = event.event_id,
                    location_id    = location_id,
                    location_name  = location_name,
                    severity       = severity,
                    threat_score   = final_score,
                    classification = classification,
                    transcript     = stored_text,
                    audio_url      = f"/api/events/{event.event_id}/audio",
                    timestamp      = event.timestamp.isoformat(),
                    emotion            = emotion,
                    emotion_confidence = emotion_confidence,
                    stt_confidence     = stt_confidence,
                    nlp_confidence     = nlp_confidence,
                    scream_confidence  = scream_confidence,
                )

            # ── FR9/FR12: Web Push fan-out (fire-and-forget, never blocks) ────
            if self.push is not None:
                self.push.fire_and_forget(
                    alert_id      = alert.alert_id,
                    event_id      = event.event_id,
                    location_id   = location_id,   # FR16: route to assigned staff
                    location_name = location_name,
                    severity      = severity,
                    timestamp     = event.timestamp.isoformat(),
                )

            logger.warning(
                f"ALERT FIRED | severity={severity} | "
                f"score={final_score} | location={location_name} | "
                f"transcript='{stored_text[:60]}'"
            )
        else:
            logger.info(
                f"Event {event.event_id}: score={final_score:.3f} "
                f"below threshold {self.threshold} and no scream — logged, no alert."
            )

        return {
            "transcript":     stored_text,
            "threat_score":   final_score,
            "severity":       severity,
            "classification": classification,
            "alert_fired":    alert_fired,
            "alert_id":       alert_id,
            "emotion":            emotion,
            "emotion_confidence": emotion_confidence,
            "stt_confidence":     stt_confidence,
            "nlp_confidence":     nlp_confidence,
            "scream_confidence":  scream_confidence,
        }

    @staticmethod
    def _safe_nearest_staff(db: Session, location_id: str):
        """
        FR30 — compute the nearest-staff hint for the live broadcast, guarded so
        a ranking error never blocks an alert. Returns None on any failure
        (display-side only; FR16 delivery routing is unaffected).
        """
        try:
            return compute_nearest_staff(db, location_id)
        except Exception as e:
            logger.warning(f"FR30 nearest-staff computation failed: {e} — sending None.")
            return None

    def _cleanup_local_working_copy(self, file_ref: str, local_path: str) -> None:
        """
        Remove the local working WAV once the durable copy lives remotely.
        No-op for the local backend (where file_ref IS local_path) or when
        DELETE_LOCAL_AFTER_UPLOAD is disabled.
        """
        if not self.audio_storage or not self.delete_local_after_upload:
            return
        if not self.audio_storage.is_remote(file_ref):
            return
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
                logger.info(f"Local working copy removed after upload: {local_path}")
        except Exception as e:
            logger.warning(f"Could not remove local working copy {local_path}: {e}")

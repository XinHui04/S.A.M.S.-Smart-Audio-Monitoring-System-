"""
services/stt_service.py
═══════════════════════════════════════════════════════
MODULE 2: Cloud Processing & AI Analysis — Part A (STT)
═══════════════════════════════════════════════════════

Uses Groq's free API to run Whisper Large v3 in the cloud.

Why Groq:
  - Completely free, no credit card required
  - Runs Whisper Large v3 — same model, same accuracy
  - 228x real-time speed (5-sec clip transcribes in <1 sec)
  - Supports English, Malay, and Manglish code-switching
  - Aligns with report wording: "Speech-to-Text APIs" (Section 1.4)

Setup:
  1. Go to https://console.groq.com
  2. Sign up (free, no credit card)
  3. Create an API key
  4. Add to .env: GROQ_API_KEY=gsk_...
"""
import asyncio
import math
import os
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class STTService:

    def __init__(self, api_key: str = ""):
        """
        api_key: your Groq API key (from https://console.groq.com)
                 Falls back to GROQ_API_KEY environment variable.
        """
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self._client = None   # lazy-loaded

    def _get_client(self):
        """Lazy-load Groq client on first use."""
        if self._client is None:
            if not self.api_key:
                raise ValueError(
                    "GROQ_API_KEY not set.\n"
                    "1. Go to https://console.groq.com (free, no credit card)\n"
                    "2. Create an API key\n"
                    "3. Add to .env: GROQ_API_KEY=gsk_..."
                )
            try:
                from groq import Groq
                self._client = Groq(api_key=self.api_key)
                logger.info("Groq STT client initialised.")
            except ImportError:
                raise RuntimeError(
                    "groq package not installed. Run: pip install groq"
                )
        return self._client

    async def transcribe(self, audio_path: str) -> dict:
        """
        Transcribes a WAV file using Groq's Whisper Large v3 API.

        The blocking Groq HTTP call runs in a worker thread via
        asyncio.to_thread() so the event loop is never blocked.

        Input:  file path from AudioCaptureService (Module 1 output)
        Output: { "text": str, "language": str, "segments": list,
                  "confidence": float | None  # 0–1, mean per-segment exp(avg_logprob); None if unavailable
                }
        """
        return await asyncio.to_thread(self._transcribe_sync, audio_path)

    def _transcribe_sync(self, audio_path: str) -> dict:
        """
        Blocking transcription body — runs Groq's Whisper Large v3 API call.

        language=None lets Whisper auto-detect, which handles
        Manglish / Malay-English code-switching (Section 2.1.2).
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        client = self._get_client()

        logger.info(f"Sending audio to Groq Whisper API: {audio_path}")

        with open(audio_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                file             = audio_file,
                model            = "whisper-large-v3",   # most accurate free model
                response_format  = "verbose_json",        # includes language + segments
                language         = None,                  # auto-detect: en, ms, Manglish
                temperature      = 0.0,                   # deterministic output
            )

        text     = transcription.text.strip() if transcription.text else ""
        lang     = getattr(transcription, "language", "unknown")
        segments = getattr(transcription, "segments", [])

        confidence = self._compute_confidence(segments) if text else None

        logger.info(f"Groq STT complete [{lang}]: '{text[:100]}' (confidence={confidence})")

        return {
            "text":       text,
            "language":   lang,
            "segments":   segments,
            "confidence": confidence,
        }

    def _compute_confidence(self, segments) -> float | None:
        """
        Derives a 0–1 transcription confidence from Whisper's per-segment
        avg_logprob values: confidence = mean(exp(avg_logprob)), clamped
        to [0.0, 1.0] and rounded to 4 decimals.

        Segments may come back as dicts or objects depending on the Groq
        SDK version, so avg_logprob is read defensively. Returns None if
        there are no usable segments, or if anything unexpected happens
        (never lets confidence computation break transcription output).
        """
        try:
            logprobs = []
            for seg in segments or []:
                if isinstance(seg, dict):
                    avg_logprob = seg.get("avg_logprob")
                else:
                    avg_logprob = getattr(seg, "avg_logprob", None)
                if avg_logprob is not None:
                    logprobs.append(avg_logprob)

            if not logprobs:
                return None

            mean_prob  = sum(math.exp(lp) for lp in logprobs) / len(logprobs)
            confidence = max(0.0, min(1.0, mean_prob))
            return round(confidence, 4)
        except Exception:
            logger.warning("Failed to compute STT confidence from segments", exc_info=True)
            return None

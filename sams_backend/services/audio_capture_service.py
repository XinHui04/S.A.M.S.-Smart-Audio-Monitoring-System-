"""
services/audio_capture_service.py
═══════════════════════════════════════════════════════
MODULE 1: Speech Detection & Audio Capture
═══════════════════════════════════════════════════════

Responsibilities:
  1. Receive raw audio bytes from Lee's edge device (ESP32-C3)
  2. Validate the clip (format, duration, size)
  3. Run Voice Activity Detection — confirm speech is present
  4. Preprocess audio (normalize, convert to mono 16kHz WAV)
  5. Save to Audio Object Storage
  6. Return a clean audio path ready for STT

Lee's edge device handles:
  - Scream/sound detection trigger
  - Rule-based decision engine
  - Initial audio capture

This module's job starts AFTER Lee sends the HTTP POST.
"""
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Target format for Whisper STT (Module 2 expects this)
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS    = 1          # mono


class AudioCaptureService:

    def __init__(
        self,
        storage_dir:       str   = "./audio_storage",
        vad_threshold:     float = 0.01,   # RMS energy — below = silence/noise only
        max_duration_secs: float = 10.0,
    ):
        self.storage_dir       = storage_dir
        self.vad_threshold     = vad_threshold
        self.max_duration_secs = max_duration_secs
        os.makedirs(self.storage_dir, exist_ok=True)

    # ── Public entry point ────────────────────────────────────────────────────

    def receive_and_prepare(
        self,
        audio_bytes:       bytes,
        original_filename: str,
        event_id:          str,
    ) -> Tuple[str, float, bool]:
        """
        Full Module 1 pipeline on a received audio clip.

        Returns:
            (file_path, duration_seconds, speech_detected)

        speech_detected = False means the clip is silence/ambient noise —
        the pipeline will skip STT and discard the event.
        """
        # Step 1: Basic validation
        self._validate(audio_bytes, original_filename)

        # Step 2: Load audio as numpy array
        audio_array, sample_rate = self._load_audio(audio_bytes, original_filename)

        # Step 3: Voice Activity Detection
        speech_detected = self._vad(audio_array, sample_rate)
        if not speech_detected:
            logger.info("VAD: No speech detected in clip — will discard event.")

        # Step 4: Preprocess (normalize, resample to 16kHz mono)
        processed_array = self._preprocess(audio_array, sample_rate)

        # Step 5: Save preprocessed WAV to object storage
        duration  = len(processed_array) / TARGET_SAMPLE_RATE
        file_path = self._save_wav(processed_array, event_id)

        logger.info(
            f"Audio capture complete | event={event_id} | "
            f"duration={duration:.2f}s | speech={speech_detected}"
        )
        return file_path, round(duration, 3), speech_detected

    def get_bytes(self, file_path: str) -> bytes:
        """Returns raw audio bytes for dashboard playback."""
        with open(file_path, "rb") as f:
            return f.read()

    # ── Internal steps ────────────────────────────────────────────────────────

    def _validate(self, audio_bytes: bytes, filename: str):
        if not audio_bytes:
            raise ValueError("Empty audio file received")
        if len(audio_bytes) > 10 * 1024 * 1024:
            raise ValueError("Audio file too large (max 10 MB)")
        ext = Path(filename).suffix.lower()
        if ext not in (".wav", ".mp3", ".ogg", ".m4a", ""):
            raise ValueError(f"Unsupported audio format: {ext}")

    def _load_audio(self, audio_bytes: bytes, filename: str) -> Tuple[np.ndarray, int]:
        """Load audio bytes into a float32 numpy array using librosa."""
        try:
            import librosa
            with tempfile.NamedTemporaryFile(
                suffix=Path(filename).suffix or ".wav", delete=False
            ) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            try:
                # librosa loads as float32, resamples to mono by default
                array, sr = librosa.load(tmp_path, sr=None, mono=False)
                return array, sr
            finally:
                os.unlink(tmp_path)
        except ImportError:
            raise RuntimeError("librosa not installed. Run: pip install librosa")

    def _vad(self, audio_array: np.ndarray, sample_rate: int) -> bool:
        """
        Voice Activity Detection using RMS energy.

        Splits the audio into 30ms frames and checks whether enough
        frames exceed the energy threshold — avoids triggering on a
        single noise spike. Requires >20% of frames to be active.
        """
        # Ensure mono for VAD
        mono = audio_array if audio_array.ndim == 1 else audio_array.mean(axis=0)

        frame_length = int(sample_rate * 0.03)   # 30ms frames
        if len(mono) < frame_length:
            return False

        # Compute RMS per frame
        num_frames   = len(mono) // frame_length
        frames       = mono[:num_frames * frame_length].reshape(num_frames, frame_length)
        rms_per_frame = np.sqrt(np.mean(frames ** 2, axis=1))

        active_frames = np.sum(rms_per_frame > self.vad_threshold)
        active_ratio  = active_frames / num_frames

        logger.debug(
            f"VAD | threshold={self.vad_threshold} | "
            f"active_frames={active_frames}/{num_frames} ({active_ratio:.1%})"
        )
        return active_ratio > 0.20   # at least 20% of frames are voiced

    def _preprocess(self, audio_array: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        Resample to 16 kHz mono, normalize amplitude.
        Whisper (Module 2) requires 16 kHz mono float32.
        """
        import librosa

        # Convert stereo → mono
        mono = audio_array if audio_array.ndim == 1 else audio_array.mean(axis=0)

        # Resample to 16 kHz if needed
        if sample_rate != TARGET_SAMPLE_RATE:
            mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=TARGET_SAMPLE_RATE)

        # Normalize to [-1, 1] — avoids clipping artifacts in Whisper
        peak = np.max(np.abs(mono))
        if peak > 0:
            mono = mono / peak

        # Enforce max duration
        max_samples = int(self.max_duration_secs * TARGET_SAMPLE_RATE)
        mono = mono[:max_samples]

        return mono.astype(np.float32)

    def _save_wav(self, audio_array: np.ndarray, event_id: str) -> str:
        """Saves preprocessed float32 array as a 16-bit PCM WAV file."""
        import soundfile as sf

        file_path = os.path.join(self.storage_dir, f"{event_id}.wav")
        sf.write(file_path, audio_array, TARGET_SAMPLE_RATE, subtype="PCM_16")
        logger.info(f"Audio saved to object storage: {file_path}")
        return file_path

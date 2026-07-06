"""
services/ser_service.py
═══════════════════════════════════════════════════════
MODULE 2: Cloud Processing & AI Analysis — Speech Emotion Recognition (SER)
═══════════════════════════════════════════════════════

Report §2.1.3: analyses the *tone* of the captured speech, complementing
scream detection (acoustic) and NLP (semantic). A distressed or angry tone
can escalate a borderline transcript into an alert.

Model: superb/wav2vec2-base-superb-er (SUPERB emotion-recognition head on
wav2vec2-base, trained on IEMOCAP). 4 classes: ang / hap / neu / sad.
~380 MB, downloads on first run, ~0.4 s inference on CPU for a 6 s clip.

Design mirrors nlp_service.py: lazy model load + graceful fallback — a SER
failure must NEVER break the pipeline, so analyse() returns None on any
error and the caller proceeds exactly as before.
"""
import os

# ── Force the PyTorch backend for transformers ───────────────────────────────
# tensorflow 2.16 (installed for the scream model) bundles Keras 3, which
# breaks transformers 4.41's TF backend — must be set BEFORE transformers loads
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")

import logging

logger = logging.getLogger(__name__)

# Map the terse SUPERB labels (and the 8-class ehcalabres alternative's full
# words, which pass through unchanged) to full lowercase emotion words.
_LABEL_MAP = {
    "ang": "angry",
    "hap": "happy",
    "neu": "neutral",
    "sad": "sad",
    # ehcalabres/wav2vec2-lg-xlsr-en-speech-emotion-recognition labels are
    # already full words (angry, calm, disgust, fearful, happy, neutral,
    # sad, surprised) — normalised to lowercase below.
}

# Emotions whose vocal tone corroborates a threat/distress situation.
# Kept here (not config) by design — this is model semantics, not tuning.
NEGATIVE_EMOTIONS = frozenset({"angry", "fearful"})

TARGET_SAMPLE_RATE = 16_000


def _normalise_label(label: str) -> str:
    label = label.strip().lower()
    return _LABEL_MAP.get(label, label)


class SERService:

    def __init__(self, model_name: str = "superb/wav2vec2-base-superb-er"):
        self.model_name = model_name
        self._pipeline  = None   # lazy-loaded

    def _load(self):
        if self._pipeline is None:
            try:
                from transformers import pipeline
                logger.info(f"Loading SER model: {self.model_name}")
                self._pipeline = pipeline(
                    "audio-classification",
                    model = self.model_name,
                    top_k = None,          # return every class score
                )
                logger.info("SER model ready.")
            except ImportError:
                raise RuntimeError(
                    "transformers not installed. Run: pip install transformers torch"
                )
        return self._pipeline

    async def analyse(self, wav_path: str) -> dict | None:
        """
        Classifies the dominant vocal emotion in a WAV file.

        Input:  path to the local working WAV (same temp file used for STT)
        Output: {"emotion": str, "confidence": float, "all_scores": {label: score}}
                or None on ANY failure — SER must never break the pipeline.
        """
        try:
            if not os.path.exists(wav_path):
                logger.warning(f"SER skipped — file not found: {wav_path}")
                return None

            # Decode via librosa (16 kHz mono float32) rather than relying on
            # the transformers pipeline's ffmpeg dependency, and hand the raw
            # array to the pipeline directly.
            import librosa
            audio, _sr = librosa.load(wav_path, sr=TARGET_SAMPLE_RATE, mono=True)
            if audio.size == 0:
                logger.warning("SER skipped — empty audio.")
                return None

            pipe   = self._load()
            output = pipe(audio, sampling_rate=TARGET_SAMPLE_RATE)
            # output: [{"label": str, "score": float}, ...] sorted by score desc
            if not output:
                logger.warning("SER model returned no scores.")
                return None

            all_scores = {
                _normalise_label(item["label"]): round(float(item["score"]), 4)
                for item in output
            }
            top = max(all_scores, key=all_scores.get)

            logger.info(
                f"SER result | emotion={top} | confidence={all_scores[top]:.3f} | "
                f"scores={all_scores}"
            )
            return {
                "emotion":    top,
                "confidence": all_scores[top],
                "all_scores": all_scores,
            }

        except Exception as e:
            logger.warning(f"SER inference failed ({e}) — continuing without emotion.")
            return None
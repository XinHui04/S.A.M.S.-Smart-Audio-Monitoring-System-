"""
services/ser_service.py
═══════════════════════════════════════════════════════
MODULE 2: Cloud Processing & AI Analysis — Speech Emotion Recognition (SER)
═══════════════════════════════════════════════════════

Report §2.1.3: analyses the *tone* of the captured speech, complementing
scream detection (acoustic) and NLP (semantic). A distressed or angry tone
can escalate a borderline transcript into an alert.

Model: Wiam/wav2vec2-lg-xlsr-en-speech-emotion-recognition-finetuned-ravdess-v8
(wav2vec2-large-xlsr fine-tuned on RAVDESS). 8 classes: angry / calm / disgust
/ fearful / happy / neutral / sad / surprised. ~1.2 GB, downloads on first run.
Chosen over ehcalabres/...ser (identical labels) because that checkpoint's
classifier head does not load on current transformers — its weights are
silently randomised, collapsing every prediction to ~uniform 0.125.

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

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

# Translation table, NOT the class list — the classes come from the model's own
# config (id2label). The default model's 8 labels are already full words (angry,
# calm, disgust, fearful, happy, neutral, sad, surprised) so they pass through
# _normalise_label() untouched. These entries exist only for the terse-label
# SUPERB fallback (SER_MODEL=superb/wav2vec2-base-superb-er), which abbreviates.
# _LABEL_MAP = {
#     "ang": "angry",
#     "hap": "happy",
#     "neu": "neutral",
#     "sad": "sad",
# }

# Emotions whose vocal tone corroborates a threat/distress situation.
# Kept here (not config) by design — this is model semantics, not tuning.
NEGATIVE_EMOTIONS = frozenset({"angry", "fearful"})

TARGET_SAMPLE_RATE = 16_000


def _normalise_label(label: str) -> str:
    label = label.strip().lower()
    # return _LABEL_MAP.get(label, label)
    return label


class SERService:

    def __init__(self, model_name: str = "Wiam/wav2vec2-lg-xlsr-en-speech-emotion-recognition-finetuned-ravdess-v8"):
        self.model_name = model_name
        self._pipeline  = None   # lazy-loaded
        # Guards lazy loading AND inference: prevents double-loading the model
        # on concurrent first requests, and the HF pipeline is not thread-safe
        # so it must never run from two threads at once.
        self._lock      = threading.Lock()

    def _load(self):
        if self._pipeline is None:
            try:
                from transformers import pipeline
                logger.info(f"Loading SER model: {self.model_name}")
                self._pipeline = pipeline(
                    "audio-classification",
                    model = self.model_name,
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

        The blocking work (lazy model load, librosa decode, inference) runs
        in a worker thread via asyncio.to_thread() so the event loop is
        never blocked.

        Input:  path to the local working WAV (same temp file used for STT)
        Output: {"emotion": str, "confidence": float, "all_scores": {label: score}}
                or None on ANY failure — SER must never break the pipeline.
        """
        return await asyncio.to_thread(self._analyse_sync, wav_path)

    def _analyse_sync(self, wav_path: str) -> dict | None:
        """
        Blocking analysis body. Returns None on ANY failure, never raises.
        The lock covers lazy loading AND the pipeline call so concurrent
        first-requests can't double-load the model and the pipeline is never
        used from two threads at once.
        """
        with self._lock:
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
                output = pipe(
                    audio,
                    sampling_rate = TARGET_SAMPLE_RATE,
                    top_k         = pipe.model.config.num_labels,
                )
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
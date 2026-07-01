"""
services/scream_analyzer.py
═══════════════════════════════════════════════════════
Cloud-based scream detection using TensorFlow Lite
For ESP32-C3 that can't run TFLite locally
═══════════════════════════════════════════════════════
"""


import numpy as np
import soundfile as sf
import tempfile
import os
import logging
from pathlib import Path
from typing import Tuple, Dict, Any


logger = logging.getLogger(__name__)


# Try to import TFLite
try:
    import tensorflow as tf
    TFLITE_AVAILABLE = True
except ImportError:
    TFLITE_AVAILABLE = False
    logger.warning("TensorFlow not installed. Using fallback detection.")


# Feature extraction parameters (same as ESP32 code)
FFT_LENGTH = 512
FRAME_LENGTH = 400
FRAME_STEP = 160
MEL_BINS = 40
LOWER_FREQ = 20
UPPER_FREQ = 4000
SAMPLE_RATE = 16000
# MEL_FRAMES = (SAMPLE_RATE - FRAME_LENGTH) // FRAME_STEP + 1
DURATION = 1  # seconds
SAMPLES = int(SAMPLE_RATE * DURATION)
MEL_FRAMES = (SAMPLES - FRAME_LENGTH) // FRAME_STEP + 1



class ScreamAnalyzer:
    """Cloud-based scream detector using TFLite"""
   
    def __init__(self, model_path: str = "models/sams_int89.tflite"):
        self.model_path = model_path
        self.model = None
       
        if TFLITE_AVAILABLE and Path(model_path).exists():
            self._load_model()
       
    def _load_model(self):
        """Load TFLite model"""
        try:
            self.model = tf.lite.Interpreter(model_path=self.model_path)
            self.model.allocate_tensors()
            self.input_details = self.model.get_input_details()
            self.output_details = self.model.get_output_details()
            logger.info(f"TFLite model loaded from {self.model_path}")
        except Exception as e:
            logger.error(f"Failed to load TFLite model: {e}")
            self.model = None
   
    def _extract_features(self, audio: np.ndarray) -> np.ndarray:
        """
        Feature extraction that EXACTLY matches training code
        """


        # ==================================================
        # FIX #2
        # Match training duration exactly (1 second)
        # ==================================================
        if len(audio) > SAMPLE_RATE:
            audio = audio[:SAMPLE_RATE]


        if len(audio) < SAMPLE_RATE:
            audio = np.pad(
                audio,
                (0, SAMPLE_RATE - len(audio)),
                mode="constant"
            )


        audio = audio.astype(np.float32)


        # ==================================================
        # FIX #1
        # STFT identical to training
        # ==================================================
        stft = tf.signal.stft(
            audio,
            frame_length=FRAME_LENGTH,
            frame_step=FRAME_STEP,
            fft_length=512
        )


        spectrogram = tf.abs(stft)


        # ==================================================
        # Mel matrix identical to training
        # ==================================================
        mel_matrix = tf.signal.linear_to_mel_weight_matrix(
            num_mel_bins=MEL_BINS,
            num_spectrogram_bins=spectrogram.shape[-1],
            sample_rate=SAMPLE_RATE,
            lower_edge_hertz=LOWER_FREQ,
            upper_edge_hertz=UPPER_FREQ
        )


        mel_spectrogram = tf.matmul(
            spectrogram,
            mel_matrix
        )


        # ==================================================
        # Log identical to training
        # ==================================================
        log_mel = tf.math.log(
            mel_spectrogram + 1e-6
        )


        # ==================================================
        # FIX #3
        # Per-frame normalization identical to training
        # ==================================================
        # mean = tf.reduce_mean(
        #     log_mel,
        #     axis=-1,
        #     keepdims=True
        # )


        # std = tf.math.reduce_std(
        #     log_mel,
        #     axis=-1,
        #     keepdims=True
        # )

        # log_mel = (
        #     log_mel - mean
        # ) / (
        #     std + 1e-6
        # )

        mean = tf.reduce_mean(log_mel)
        std = tf.math.reduce_std(log_mel)
        log_mel = (log_mel - mean) / (std + 1e-6)

        features = log_mel.numpy().astype(np.float32)


        logger.info(
            f"Feature shape: {features.shape}"
        )


        logger.info(
            f"Feature stats: "
            f"min={features.min():.3f}, "
            f"max={features.max():.3f}, "
            f"mean={features.mean():.3f}, "
            f"std={features.std():.3f}"
        )


        return features.reshape(
            1,
            MEL_FRAMES,
            MEL_BINS,
            1
        )    


    def _extract_audio_array(self, audio_bytes: bytes) -> np.ndarray:
        """
        Extract audio array from bytes for saving to storage.
        """
        try:
            # Check if we have valid WAV or raw PCM
            if len(audio_bytes) < 44:
                logger.warning(f"Audio too short: {len(audio_bytes)} bytes")
                return np.zeros(SAMPLE_RATE, dtype=np.float32)
           
            # Check WAV header
            riff = audio_bytes[0:4]
            wave = audio_bytes[8:12]
           
            if riff == b'RIFF' and wave == b'WAVE':
                # Valid WAV - try to read with soundfile
                # try:
                #     with tempfile.NamedTemporaryFile(suffix='.wav', delete=True) as tmp:
                #         tmp.write(audio_bytes)
                #         tmp.flush()
                #         audio, sr = sf.read(tmp.name)
                # except Exception as e:
                #     logger.warning(f"soundfile failed, reading raw PCM: {e}")
                #     # Read raw PCM from offset 44
                #     audio = np.frombuffer(audio_bytes[44:], dtype=np.int16).astype(np.float32) / 32768.0
                #     sr = SAMPLE_RATE
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                        tmp.write(audio_bytes)
                        tmp_path = tmp.name
                    audio, sr = sf.read(tmp_path)   # file is closed now, safe to read
                except Exception as e:
                    logger.warning(f"soundfile failed, reading raw PCM: {e}")
                    audio = np.frombuffer(audio_bytes[44:], dtype=np.int16).astype(np.float32) / 32768.0
                    sr = SAMPLE_RATE
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
            else:
                # Read as raw PCM
                logger.info("Reading as raw PCM")
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                sr = SAMPLE_RATE
           
            # Resample to 16kHz if needed
            if sr != SAMPLE_RATE:
                try:
                    import librosa
                    audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
                except ImportError:
                    pass
           
            # Convert stereo to mono
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
           
            # Ensure we have at least 1 second
            if len(audio) < SAMPLE_RATE:
                audio = np.pad(audio, (0, SAMPLE_RATE - len(audio)), 'constant')
           
            return audio.astype(np.float32)
           
        except Exception as e:
            logger.error(f"Error extracting audio array: {e}")
            return np.zeros(SAMPLE_RATE, dtype=np.float32)
       
    def analyze(self, audio_bytes: bytes) -> Dict[str, Any]:
        """
        Analyze audio bytes for scream detection
       
        Returns:
            {
                'confidence': float (0-1),
                'is_scream': bool,
                'error': str or None
            }
        """
        try:
            # ── Check if we have valid WAV or raw PCM ──────────────────────────
            if len(audio_bytes) < 44:
                logger.error(f"Audio too short: {len(audio_bytes)} bytes")
                return {
                    'confidence': 0.0,
                    'is_scream': False,
                    'error': 'Audio too short'
                }
           
            # Check if it's a valid WAV file (has RIFF header)
            riff = audio_bytes[0:4]
            wave = audio_bytes[8:12]
           
            if riff == b'RIFF' and wave == b'WAVE':
                # Valid WAV file - try to read with soundfile first
                # try:
                #     with tempfile.NamedTemporaryFile(suffix='.wav', delete=True) as tmp:
                #         tmp.write(audio_bytes)
                #         tmp.flush()
                #         audio, sr = sf.read(tmp.name)
                # except Exception as e:
                #     logger.warning(f"soundfile failed, reading raw PCM: {e}")
                #     # Read raw PCM from the data chunk
                #     # WAV header is 44 bytes, data starts at offset 44
                #     audio = np.frombuffer(audio_bytes[44:], dtype=np.int16).astype(np.float32) / 32768.0
                #     sr = SAMPLE_RATE
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                        tmp.write(audio_bytes)
                        tmp_path = tmp.name
                    audio, sr = sf.read(tmp_path)   # file is closed now, safe to read
                except Exception as e:
                    logger.warning(f"soundfile failed, reading raw PCM: {e}")
                    audio = np.frombuffer(audio_bytes[44:], dtype=np.int16).astype(np.float32) / 32768.0
                    sr = SAMPLE_RATE
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
            else:
                # Not a valid WAV - try to read as raw PCM
                logger.info("Reading as raw PCM (no WAV header)")
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                sr = SAMPLE_RATE
           
            # ── Resample to 16kHz if needed ──────────────────────────────────────
            if sr != SAMPLE_RATE:
                try:
                    import librosa
                    audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
                except ImportError:
                    logger.warning("librosa not installed, skipping resample")
           
            # Convert stereo to mono
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
           
            # Ensure we have at least 1 second of audio
            if len(audio) < SAMPLE_RATE:
                logger.warning(f"Audio too short: {len(audio)} samples, padding...")
                audio = np.pad(audio, (0, SAMPLE_RATE - len(audio)), 'constant')
           
            logger.info(f"Audio loaded: {len(audio)} samples, sr={sr}")
           
            # ── Extract features ──────────────────────────────────────────────────
            features = self._extract_features(audio)
           
            # ── Run inference ─────────────────────────────────────────────────────
            if self.model is not None:
                try:
                    logger.info(
                        f"Input shape: {features.shape}"
                    )


                    logger.info(
                        f"Input min={features.min():.4f}, "
                        f"max={features.max():.4f}, "
                        f"mean={features.mean():.4f}"
                    )


                    self.model.set_tensor(self.input_details[0]['index'], features.astype(np.float32))
                    self.model.invoke()
                    # output = self.model.get_tensor(self.output_details[0]['index'])
                    # logger.info(f"Output shape: {output.shape}")
                    # logger.info(f"Output value: {output}")
                    # confidence = float(output[0][1]) if output.shape[1] > 1 else float(output[0][0])
                    # logger.info(f"TFLite inference complete: confidence={confidence:.3f}")
                    output = self.model.get_tensor(
                        self.output_details[0]['index']
                    )


                    logger.info(f"Output shape: {output.shape}")
                    logger.info(f"Output raw: {output}")


                    out_detail = self.output_details[0]


                    if out_detail["dtype"] != np.float32:


                        scale, zero_point = out_detail["quantization"]


                        logger.info(
                            f"Output quantization: "
                            f"scale={scale}, "
                            f"zero_point={zero_point}"
                        )


                        confidence = (
                            float(output.flatten()[0]) - zero_point
                        ) * scale


                    else:
                        confidence = float(output.flatten()[0])


                    logger.info(
                        f"Confidence extracted = {confidence:.6f}"
                    )


                    logger.info(
                        f"Input details: {self.input_details}"
                    )


                    logger.info(
                        f"Output details: {self.output_details}"
                    )


                except Exception as e:
                    logger.error(f"TFLite inference failed: {e}")
                    # Fallback to energy detection
                    rms = np.sqrt(np.mean(audio ** 2))
                    confidence = min(1.0, rms * 10.0)
                    logger.info(f"Using fallback detection: confidence={confidence:.3f}")
            else:
                # Fallback: use simple energy-based detection
                rms = np.sqrt(np.mean(audio ** 2))
                confidence = min(1.0, rms * 10.0)
                logger.info(f"Fallback detection: rms={rms:.4f}, confidence={confidence:.3f}")
           
            # ── Determine if scream ──────────────────────────────────────────────
            # is_scream = confidence >= 0.50
            BEST_THRESHOLD = 0.30  # example from training logs
            is_scream = bool(confidence >= BEST_THRESHOLD)


            return {
                'confidence': confidence,
                'is_scream': is_scream,
                'error': None
            }
           
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            import traceback
            traceback.print_exc()
            return {
                'confidence': 0.0,
                'is_scream': False,
                'error': str(e)
            }
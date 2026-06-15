"""
services/storage_service.py

YOUR MODULE: Cloud Processing and AI Analysis (storage component)

Stores audio clips received from Lee's edge device.
Separates audio binary storage from the relational database,
matching the two-node Data Management layer in Figure 4.1:
  [Relational Database]  [Audio Object Storage]

Dev:  local ./audio_storage/ folder, returns a file path
Prod: swap USE_CLOUD_STORAGE=true to push to Firebase Storage
"""
import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AudioStorageService:

    def __init__(self, storage_dir: str = "./audio_storage"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)

    def save(self, audio_bytes: bytes, event_id: str, original_filename: str) -> str:
        """
        Saves audio bytes, returns the stored path.
        The path is what gets recorded in AudioClip.file_path in the DB.
        """
        ext           = Path(original_filename).suffix or ".wav"
        safe_name     = f"{event_id}{ext}"
        file_path     = os.path.join(self.storage_dir, safe_name)

        with open(file_path, "wb") as f:
            f.write(audio_bytes)

        logger.info(f"Audio saved: {file_path} ({len(audio_bytes):,} bytes)")
        return file_path

    def get_bytes(self, file_path: str) -> Optional[bytes]:
        """Retrieves audio bytes for dashboard playback."""
        if not os.path.exists(file_path):
            logger.warning(f"Audio not found: {file_path}")
            return None
        with open(file_path, "rb") as f:
            return f.read()

    def delete(self, file_path: str) -> bool:
        """
        Deletes stored audio after processing if privacy mode enabled.
        Aligns with Security Design (Section 4.6): audio not permanently archived.
        """
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Audio deleted: {file_path}")
                return True
        except Exception as e:
            logger.error(f"Delete failed: {e}")
        return False

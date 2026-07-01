"""
services/storage_service.py

YOUR MODULE: Cloud Processing and AI Analysis (storage component)

Stores audio clips received from the edge device. Separates audio binary storage
from the relational database, matching the two-node Data Management layer:
  [Relational Database]  [Audio Object Storage]

Two backends behind one "ref" abstraction. A ref is what gets written to
AudioClip.file_path:
  - local    : the ref is a local filesystem path (default; dev/offline/tests)
  - supabase : the ref is "supabase://<bucket>/<object>" pointing at Supabase Storage

Switch with STORAGE_BACKEND=supabase + SUPABASE_URL + SUPABASE_SERVICE_KEY.
"""
import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REMOTE_PREFIX = "supabase://"


class AudioStorageService:

    def __init__(
        self,
        backend:      str = "local",
        storage_dir:  str = "./audio_storage",
        supabase_url: str = "",
        supabase_key: str = "",
        bucket_name:  str = "audio-clips",
    ):
        self.backend     = (backend or "local").lower()
        self.storage_dir = storage_dir
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.bucket_name = bucket_name
        self._client      = None   # lazy Supabase client

        os.makedirs(self.storage_dir, exist_ok=True)

        if self.backend == "supabase" and not (supabase_url and supabase_key):
            logger.warning(
                "STORAGE_BACKEND=supabase but SUPABASE_URL/SUPABASE_SERVICE_KEY "
                "missing — falling back to local disk storage."
            )
            self.backend = "local"

        logger.info(f"AudioStorageService backend: {self.backend}")

    # ── Supabase client (lazy) ────────────────────────────────────────────────

    def _get_client(self):
        if self._client is None:
            try:
                from supabase import create_client
            except ImportError:
                raise RuntimeError(
                    "supabase package not installed. Run: pip install supabase"
                )
            self._client = create_client(self.supabase_url, self.supabase_key)
            logger.info("Supabase Storage client initialised.")
        return self._client

    # ── Ref helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def is_remote(file_ref: str) -> bool:
        return bool(file_ref) and file_ref.startswith(REMOTE_PREFIX)

    @staticmethod
    def _parse_ref(file_ref: str):
        """'supabase://bucket/path/to/obj.wav' -> ('bucket', 'path/to/obj.wav')"""
        body = file_ref[len(REMOTE_PREFIX):]
        bucket, _, obj = body.partition("/")
        return bucket, obj

    # ── Public API ────────────────────────────────────────────────────────────

    def persist(self, local_path: str, event_id: str) -> str:
        """
        Persist a local working WAV to durable storage and return the ref to
        store in AudioClip.file_path.

        local backend    -> returns local_path unchanged (the durable copy IS local)
        supabase backend -> uploads bytes to <bucket>/<event_id>.wav, returns the
                            'supabase://...' ref. On any failure, logs and falls
                            back to returning the local path (never loses the clip).
        """
        if self.backend != "supabase":
            return local_path

        ext        = Path(local_path).suffix or ".wav"
        object_key = f"{event_id}{ext}"
        try:
            with open(local_path, "rb") as f:
                data = f.read()
            client = self._get_client()
            client.storage.from_(self.bucket).upload(
                path=object_key,
                file=data,
                file_options={"content-type": "audio/wav", "upsert": "true"},
            )
            ref = f"{REMOTE_PREFIX}{self.bucket}/{object_key}"
            logger.info(f"Audio uploaded to Supabase Storage: {ref} ({len(data):,} bytes)")
            return ref
        except Exception as e:
            logger.error(f"Supabase upload failed ({e}) — keeping local path: {local_path}")
            return local_path

    def get_bytes(self, file_ref: str) -> Optional[bytes]:
        """Retrieve audio bytes for dashboard playback, from local disk or Supabase."""
        try:
            if self.is_remote(file_ref):
                bucket, obj = self._parse_ref(file_ref)
                client = self._get_client()
                return client.storage.from_(bucket).download(obj)
            if not os.path.exists(file_ref):
                logger.warning(f"Audio not found: {file_ref}")
                return None
            with open(file_ref, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Audio fetch failed for {file_ref}: {e}")
            return None

    def delete(self, file_ref: str) -> bool:
        """Delete stored audio (privacy mode). Handles both backends."""
        try:
            if self.is_remote(file_ref):
                bucket, obj = self._parse_ref(file_ref)
                self._get_client().storage.from_(bucket).remove([obj])
                logger.info(f"Audio deleted from Supabase: {file_ref}")
                return True
            if os.path.exists(file_ref):
                os.remove(file_ref)
                logger.info(f"Audio deleted: {file_ref}")
                return True
        except Exception as e:
            logger.error(f"Delete failed for {file_ref}: {e}")
        return False
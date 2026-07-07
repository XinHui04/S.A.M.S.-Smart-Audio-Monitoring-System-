"""
tests/test_storage_service.py
Unit tests for AudioStorageService: persist(), get_bytes() and the
_safe_local_path() path-traversal guard.

No network / no real Supabase — the Supabase client is stubbed. Filesystem
work uses pytest tmp_path so tests are hermetic.

Run with:
    pytest tests/ -v
"""
import os

import pytest

from services.storage_service import AudioStorageService, REMOTE_PREFIX


# ─── Fakes ───────────────────────────────────────────────────────────────────

class _FakeUploadTarget:
    """Records the bucket it was created from and the upload call args."""
    def __init__(self, recorder, bucket):
        self.recorder = recorder
        self.bucket = bucket

    def upload(self, path, file, file_options=None):
        self.recorder["bucket"] = self.bucket
        self.recorder["path"] = path
        self.recorder["file"] = file
        return {"path": path}


class _FakeStorage:
    def __init__(self, recorder):
        self.recorder = recorder

    def from_(self, bucket):
        return _FakeUploadTarget(self.recorder, bucket)


class _FakeClient:
    def __init__(self, recorder):
        self.storage = _FakeStorage(recorder)


# ─── persist() ───────────────────────────────────────────────────────────────

def test_persist_local_returns_input_path(tmp_path):
    svc = AudioStorageService(backend="local", storage_dir=str(tmp_path))
    p = os.path.join(str(tmp_path), "abc.wav")
    assert svc.persist(p, "abc") == p


def test_persist_supabase_uploads_and_returns_ref(tmp_path):
    svc = AudioStorageService(
        backend="supabase",
        storage_dir=str(tmp_path),
        supabase_url="https://example.supabase.co",
        supabase_key="service-key",
        bucket_name="audio-clips",
    )
    # backend stays "supabase" because url+key are provided
    assert svc.backend == "supabase"

    recorder = {}
    svc._client = _FakeClient(recorder)  # bypass real network client

    local_file = tmp_path / "clip.wav"
    local_file.write_bytes(b"RIFFfake-wav-data")

    ref = svc.persist(str(local_file), "event-123")

    assert ref == f"{REMOTE_PREFIX}audio-clips/event-123.wav"
    # Proves the self.bucket bug is fixed: upload used bucket_name.
    assert recorder["bucket"] == "audio-clips"
    assert recorder["path"] == "event-123.wav"
    assert recorder["file"] == b"RIFFfake-wav-data"


# ─── get_bytes() local reads ─────────────────────────────────────────────────

def test_get_bytes_reads_file_inside_storage_dir(tmp_path):
    svc = AudioStorageService(backend="local", storage_dir=str(tmp_path))
    f = tmp_path / "inside.wav"
    f.write_bytes(b"local-audio-bytes")

    assert svc.get_bytes(str(f)) == b"local-audio-bytes"


def test_get_bytes_rejects_parent_traversal(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"TOP-SECRET-BYTES")

    svc = AudioStorageService(backend="local", storage_dir=str(storage))

    # A '../'-style ref that resolves to the secret outside storage_dir.
    ref = os.path.join("..", "secret.wav")
    result = svc.get_bytes(ref)

    assert result is None
    assert result != b"TOP-SECRET-BYTES"


def test_get_bytes_rejects_absolute_path_outside_storage(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"TOP-SECRET-BYTES")

    svc = AudioStorageService(backend="local", storage_dir=str(storage))

    result = svc.get_bytes(str(secret))  # absolute path, outside storage_dir

    assert result is None


# ─── _safe_local_path() unit test ────────────────────────────────────────────

def test_safe_local_path_accepts_legit_and_rejects_escape(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    svc = AudioStorageService(backend="local", storage_dir=str(storage))

    # Legit file inside storage_dir is accepted (real playback path).
    good = storage / "clip.wav"
    good.write_bytes(b"x")
    resolved = svc._safe_local_path(str(good))
    assert resolved is not None
    assert os.path.realpath(resolved) == os.path.realpath(str(good))

    # Relative path that stays inside is accepted.
    assert svc._safe_local_path("clip.wav") is not None

    # '../' escape is rejected.
    assert svc._safe_local_path(os.path.join("..", "secret.wav")) is None

    # Absolute path outside storage_dir is rejected.
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"y")
    assert svc._safe_local_path(str(outside)) is None

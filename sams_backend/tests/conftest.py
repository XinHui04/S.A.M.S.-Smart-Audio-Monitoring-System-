"""
tests/conftest.py
Shared test setup. Runs before any test module is imported.

Rate limiting: utils/rate_limit.py builds the limiter from the lru_cached
settings at first import, so the env var must be set BEFORE any app module is
imported. setdefault keeps it overridable from the shell if ever needed.
Tests hammer /api/auth/login far past the production 5/minute limit, so the
limiter is disabled suite-wide; tests/test_rate_limit.py re-enables it
explicitly (limiter.enabled is read per-request) to prove the 429 path.
"""
import os

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

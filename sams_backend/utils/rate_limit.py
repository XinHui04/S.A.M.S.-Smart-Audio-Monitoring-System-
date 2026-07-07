"""
utils/rate_limit.py
═══════════════════════════════════════════════════════
Shared IP-based rate limiter (slowapi).
═══════════════════════════════════════════════════════
Singleton `limiter` used as a decorator on brute-forceable / abuse-prone
endpoints (login, device ingestion). Wired in main.py via
`app.state.limiter` + the RateLimitExceeded exception handler — no global
middleware, limits apply only where explicitly decorated.

Configuration: RATE_LIMIT_ENABLED (settings.rate_limit_enabled, default True).
Tests flip `limiter.enabled` off (see tests/conftest.py); slowapi reads the
flag per-request, so it can also be toggled at runtime.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from config.settings import get_settings

# NOTE: get_settings() is lru_cached — importing this module resolves settings
# with whatever env is in place at first import (same pattern as
# api/dependencies.py). In-memory storage: per-process, resets on restart.
limiter = Limiter(
    key_func=get_remote_address,
    enabled=get_settings().rate_limit_enabled,
)

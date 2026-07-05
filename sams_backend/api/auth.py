"""
api/auth.py
═══════════════════════════════════════════════════════
FR23 — Staff Login & JWT Authentication
═══════════════════════════════════════════════════════
POST /api/auth/login → verify email + bcrypt password, issue a signed JWT.
GET  /api/auth/me    → return the profile of the currently authenticated user.

Security notes:
  - Identical 401 detail for unknown email vs wrong password (no user enumeration).
  - Passwords and tokens are never logged.
  - Tokens are signed HS256 with settings.jwt_secret_key (from .env, never committed).
"""
import logging
from datetime import datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import User
from models.schemas import LoginRequest, TokenResponse
from api.dependencies import get_db, get_current_user
from utils.auth import verify_password

router = APIRouter(prefix="/api/auth", tags=["Auth"])
logger = logging.getLogger(__name__)


def _user_out(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "name":    user.name,
        "email":   user.email,
        "role":    user.role,
    }


@router.post("/login", response_model=TokenResponse, summary="Staff/admin login — returns a JWT")
async def login(body: LoginRequest, db: Session = Depends(get_db)):
    cfg = get_settings()
    if not cfg.jwt_secret_key:
        logger.error("Login attempted but JWT_SECRET_KEY is not configured — set it in .env")
        raise HTTPException(503, "Authentication not configured")

    user = db.query(User).filter(User.email == body.email).first()
    # Same generic message whether the email is unknown or the password is
    # wrong — prevents user enumeration.
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Invalid email or password")

    now = datetime.utcnow()
    payload = {
        "sub":  user.user_id,
        "role": user.role,
        "name": user.name,
        "iat":  now,
        "exp":  now + timedelta(minutes=cfg.access_token_expire_minutes),
    }
    token = jwt.encode(payload, cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm)

    logger.info(f"[Auth] Login OK for user_id={user.user_id} role={user.role}")
    return {
        "access_token": token,
        "token_type":   "bearer",
        "user":         _user_out(user),
    }


@router.get("/me", summary="Current authenticated user's profile")
async def me(user: User = Depends(get_current_user)):
    if user is None:   # auth unconfigured (JWT_SECRET_KEY empty) — no identity
        raise HTTPException(503, "Authentication not configured")
    return _user_out(user)
"""
api/push.py
═══════════════════════════════════════════════════════
FR9 / FR12 — Web Push subscription management
═══════════════════════════════════════════════════════
Endpoints the teacher PWA calls to register/unregister a browser for Web Push:

  GET    /api/push/public-key → the VAPID public key (browser applicationServerKey)
  POST   /api/push/subscribe  → upsert this browser's push subscription
  DELETE /api/push/subscribe  → remove this browser's push subscription

Every endpoint requires a valid staff/admin JWT (the PWA is logged in before it
subscribes). Security notes:
  - Bodies are validated by Pydantic (https-only endpoint, bounded lengths).
  - A subscription can only be deleted by its owner (or an admin).
  - The private VAPID key is NEVER returned — only the public key.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.database import PushSubscription, User
from models.schemas import PushSubscribeRequest, PushUnsubscribeRequest
from api.dependencies import get_db, get_current_user

router = APIRouter(prefix="/api/push", tags=["Push — FR9/FR12"])
logger = logging.getLogger(__name__)


@router.get("/public-key", summary="VAPID public key for the browser to subscribe")
async def public_key(user: User = Depends(get_current_user)):
    if user is None:   # auth unconfigured (JWT_SECRET_KEY empty)
        raise HTTPException(503, "Authentication not configured")
    cfg = get_settings()
    return {
        "public_key": cfg.vapid_public_key,
        "enabled":    bool(cfg.vapid_public_key and cfg.vapid_private_key),
    }


@router.post("/subscribe", summary="Register this browser for Web Push")
async def subscribe(
    body: PushSubscribeRequest,
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    if user is None:
        raise HTTPException(503, "Authentication not configured")

    # Upsert by endpoint (unique). A device may re-subscribe as a different
    # logged-in user → the existing row is re-pointed to the current user.
    existing = (
        db.query(PushSubscription)
          .filter(PushSubscription.endpoint == body.endpoint)
          .first()
    )
    if existing is not None:
        existing.user_id = user.user_id
        existing.p256dh  = body.keys.p256dh
        existing.auth    = body.keys.auth
    else:
        db.add(PushSubscription(
            user_id  = user.user_id,
            endpoint = body.endpoint,
            p256dh   = body.keys.p256dh,
            auth     = body.keys.auth,
        ))
    db.commit()
    return {"status": "subscribed"}


@router.delete("/subscribe", summary="Unregister this browser from Web Push")
async def unsubscribe(
    body: PushUnsubscribeRequest,
    db:   Session = Depends(get_db),
    user: User    = Depends(get_current_user),
):
    if user is None:
        raise HTTPException(503, "Authentication not configured")

    row = (
        db.query(PushSubscription)
          .filter(PushSubscription.endpoint == body.endpoint)
          .first()
    )
    # 404 (not 403) when it isn't the caller's own subscription — the endpoint
    # existence is not enumerable by a non-owner.
    if row is None or (row.user_id != user.user_id and user.role != "admin"):
        raise HTTPException(404, "Subscription not found")

    db.delete(row)
    db.commit()
    return {"status": "unsubscribed"}

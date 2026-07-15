"""
services/push_service.py
═══════════════════════════════════════════════════════
FR9 / FR12 — Web Push notifications (PWA closed → still alerted)
═══════════════════════════════════════════════════════

When the pipeline fires an alert it also fans the alert out to every browser
that has registered a Web Push subscription, so a teacher receives it even when
the S.A.M.S. teacher PWA is closed. Delivery goes via the browser vendor's push
service (Google FCM / Mozilla autopush), authenticated with our VAPID keypair.

FR16 — routing (mirrors WebSocketManager._allowed EXACTLY, fail-open):
  - alert with no location_id      → everyone
  - user role == "admin"           → yes
  - user has NO StaffLocation rows → yes (fail-open: missing config must never
                                          hide a safety incident)
  - otherwise                      → only if location_id is in the assignments

Design guarantees (push must NEVER break the pipeline):
  - Disabled cleanly when either VAPID key is empty (one info log, never crash).
  - The whole fan-out runs OFF the event loop (asyncio.to_thread), so the async
    pipeline never blocks on network I/O.
  - Every exception is swallowed + logged; one bad subscription never affects
    the others or the pipeline.
  - Opens/closes its OWN DB session (the request session may already be closed).
  - Payload is MINIMAL — it transits third-party push services, so it carries no
    transcript and no threat score.
  - Expired/revoked subscriptions (push service returns 404/410) are pruned.
"""
import asyncio
import json
import logging
import threading
from datetime import datetime
from typing import Optional

from pywebpush import webpush, WebPushException

from models.database import PushSubscription, User, StaffLocation

logger = logging.getLogger(__name__)

# Time-to-live handed to the push service: how long it should retry delivery to
# an offline device before discarding the message (seconds).
_PUSH_TTL = 300


class PushService:

    def __init__(
        self,
        vapid_public_key:  str,
        vapid_private_key: str,
        vapid_subject:     str,
        session_factory=None,
    ):
        self.vapid_public_key  = vapid_public_key or ""
        self.vapid_private_key = vapid_private_key or ""
        self.vapid_subject     = vapid_subject or ""
        self._session_factory  = session_factory
        # Push is opt-in: without BOTH keys it stays silently disabled.
        self.enabled = bool(self.vapid_public_key and self.vapid_private_key)
        # Keep strong references to scheduled fire-and-forget tasks so the event
        # loop does not garbage-collect them before they finish.
        self._tasks: set = set()

        if self.enabled:
            logger.info("PushService: enabled (VAPID keys configured).")
        else:
            logger.info("PushService: disabled — VAPID keys not configured; "
                        "Web Push notifications will not be sent.")

    # ── FR16 routing rule — mirrors WebSocketManager._allowed exactly ─────────
    @staticmethod
    def _allowed(role: Optional[str], assigned, location_id: Optional[str]) -> bool:
        """FR16 routing rule (fail-open). `assigned` is the user's location list
        (falsy = unrestricted). `role`/`assigned` may be None when the user row
        cannot be resolved → treated as unrestricted, same fail-open intent."""
        if location_id is None:      # untagged alert → everyone
            return True
        if role == "admin":
            return True
        if not assigned:             # unassigned staff (or unknown user) → all
            return True
        return location_id in assigned

    # ── Synchronous worker (safe to call from a thread) ───────────────────────
    def send_alert(
        self,
        db_session_factory,
        *,
        alert_id:      str,
        event_id:      str,
        location_id:   Optional[str],
        location_name: Optional[str],
        severity:      str,
        timestamp:     Optional[str] = None,
    ) -> int:
        """
        Fan an alert out to every eligible push subscription. Runs its OWN DB
        session. Returns the number of pushes attempted (best-effort — a per-
        subscription failure is logged and skipped, never raised). Prunes any
        subscription the push service reports as gone (404/410).
        """
        if not self.enabled:
            return 0

        factory = db_session_factory or self._session_factory
        if factory is None:
            logger.warning("PushService.send_alert: no DB session factory — skipping.")
            return 0

        # Minimal payload — transits third-party push services. NO transcript,
        # NO threat score.
        payload = json.dumps({
            "type":          "ALERT",
            "alert_id":      alert_id,
            "severity":      severity,
            "location_name": location_name,
            "timestamp":     timestamp or datetime.utcnow().isoformat(),
        })

        db = factory()
        attempted = 0
        prune_ids: list = []
        try:
            subs = db.query(PushSubscription).all()
            if not subs:
                return 0

            # Resolve role + location assignments per user in bulk (mirrors the
            # FR16 fail-open rule used by the WebSocket/alert-feed paths).
            user_ids = {s.user_id for s in subs}
            roles = {
                u.user_id: u.role
                for u in db.query(User).filter(User.user_id.in_(user_ids)).all()
            }
            assignments: dict = {}
            for row in (db.query(StaffLocation)
                          .filter(StaffLocation.user_id.in_(user_ids)).all()):
                assignments.setdefault(row.user_id, set()).add(row.location_id)

            for sub in subs:
                if not self._allowed(roles.get(sub.user_id),
                                     assignments.get(sub.user_id),
                                     location_id):
                    continue
                attempted += 1
                try:
                    webpush(
                        subscription_info={
                            "endpoint": sub.endpoint,
                            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                        },
                        data=payload,
                        vapid_private_key=self.vapid_private_key,
                        vapid_claims={"sub": self.vapid_subject},
                        ttl=_PUSH_TTL,
                    )
                except WebPushException as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (404, 410):
                        # Subscription expired / revoked → prune it.
                        prune_ids.append(sub.subscription_id)
                        logger.info(
                            f"Push: pruning gone subscription {sub.subscription_id} "
                            f"(status {status})."
                        )
                    else:
                        logger.warning(
                            f"Push: delivery failed for subscription "
                            f"{sub.subscription_id} (status {status}) — skipping."
                        )
                except Exception as exc:  # noqa: BLE001 — never let one sub break the rest
                    logger.warning(
                        f"Push: unexpected error for subscription "
                        f"{sub.subscription_id}: {exc} — skipping."
                    )

            if prune_ids:
                db.query(PushSubscription).filter(
                    PushSubscription.subscription_id.in_(prune_ids)
                ).delete(synchronize_session=False)
                db.commit()

            logger.info(
                f"Push fan-out: ALERT severity={severity} "
                f"location={location_id or 'ALL'} attempted={attempted} "
                f"pruned={len(prune_ids)} of {len(subs)} subscriptions."
            )
        except Exception as exc:  # noqa: BLE001 — swallow: push must never break the pipeline
            logger.warning(f"Push fan-out aborted: {exc}")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
        return attempted

    # ── Async wrapper — runs the sync worker OFF the event loop ───────────────
    async def send_alert_async(
        self,
        *,
        alert_id:      str,
        event_id:      str,
        location_id:   Optional[str],
        location_name: Optional[str],
        severity:      str,
        timestamp:     Optional[str] = None,
        db_session_factory=None,
    ) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(
                self.send_alert,
                db_session_factory,
                alert_id=alert_id,
                event_id=event_id,
                location_id=location_id,
                location_name=location_name,
                severity=severity,
                timestamp=timestamp,
            )
        except Exception as exc:  # noqa: BLE001 — belt-and-braces
            logger.warning(f"Push send_alert_async failed: {exc}")

    # ── Fire-and-forget entry point used by the processing pipeline ───────────
    def fire_and_forget(
        self,
        *,
        alert_id:      str,
        event_id:      str,
        location_id:   Optional[str],
        location_name: Optional[str],
        severity:      str,
        timestamp:     Optional[str] = None,
        db_session_factory=None,
    ) -> None:
        """
        Schedule the fan-out without blocking the caller. Called from the async
        pipeline (a loop is running) → create a background task. If no loop is
        running (e.g. a sync caller), run it in a daemon thread instead. Any
        failure is swallowed — the pipeline must never fail because of push.
        """
        if not self.enabled:
            return
        try:
            coro = self.send_alert_async(
                alert_id=alert_id,
                event_id=event_id,
                location_id=location_id,
                location_name=location_name,
                severity=severity,
                timestamp=timestamp,
                db_session_factory=db_session_factory,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(coro)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                threading.Thread(
                    target=lambda: asyncio.run(coro), daemon=True
                ).start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Push fire_and_forget failed to schedule: {exc}")

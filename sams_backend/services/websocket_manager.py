"""
services/websocket_manager.py
═══════════════════════════════════════════════════════
MODULE 4: Main Computer Monitoring System — real-time push
═══════════════════════════════════════════════════════

Manages all live WebSocket connections from the dashboard browser.
When the pipeline fires an alert, it calls broadcast_alert() here,
which instantly pushes it to every connected dashboard tab.

FR16 — alert routing by role/location (FAIL-OPEN):
  - admins always receive every alert
  - staff WITH location assignments receive only alerts from those locations
  - staff with NO assignments receive everything (missing config must never
    hide a safety incident)
  - user_info=None (auth unconfigured / dev mode) receives everything
  - alerts with no location_id go to everyone

Dashboard connects to: ws://localhost:8000/ws/dashboard
"""
import json
import logging
from datetime import datetime
from typing import Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class WebSocketManager:

    def __init__(self):
        # ws → user_info dict {"user_id", "role", "location_ids"} or None
        # (None / empty location_ids = unrestricted, per the fail-open rule)
        self.connections: Dict[WebSocket, Optional[dict]] = {}

    async def connect(self, websocket: WebSocket, user_info: Optional[dict] = None):
        await websocket.accept()
        self.connections[websocket] = user_info
        who = (
            f"user={user_info.get('user_id')} role={user_info.get('role')} "
            f"locations={user_info.get('location_ids') or 'ALL'}"
            if user_info else "unauthenticated (unrestricted)"
        )
        logger.info(f"Dashboard connected ({who}). Active: {len(self.connections)}")
        await websocket.send_json({
            "type":      "CONNECTED",
            "message":   "Connected to S.A.M.S. live alert feed",
            "timestamp": datetime.utcnow().isoformat(),
        })

    def disconnect(self, websocket: WebSocket):
        self.connections.pop(websocket, None)
        logger.info(f"Dashboard disconnected. Active: {len(self.connections)}")

    @staticmethod
    def _allowed(user_info: Optional[dict], location_id: Optional[str]) -> bool:
        """FR16 routing rule — see module docstring. Fail-open by design."""
        if location_id is None:          # untagged alert → everyone
            return True
        if user_info is None:            # dev mode / auth unconfigured
            return True
        if user_info.get("role") == "admin":
            return True
        assigned = user_info.get("location_ids")
        if not assigned:                 # staff with no assignments → everything
            return True
        return location_id in assigned

    async def _broadcast(self, message: dict, location_id: Optional[str] = None) -> int:
        """
        Send a message to all connections allowed to receive it.
        Non-ALERT messages pass location_id=None → delivered to everyone.
        Returns the number of clients the message was sent to.
        """
        payload = json.dumps(message, default=str)
        dead, sent = set(), 0
        for ws, user_info in list(self.connections.items()):
            if not self._allowed(user_info, location_id):
                continue
            try:
                await ws.send_text(payload)
                sent += 1
            except (WebSocketDisconnect, Exception):
                dead.add(ws)
        for ws in dead:
            self.connections.pop(ws, None)
        return sent

    async def broadcast_alert(
        self,
        alert_id:       str,
        event_id:       str,
        location_name:  str,
        severity:       str,
        threat_score:   float,
        classification: str,
        transcript:     str,
        audio_url:      str  = None,
        timestamp:      str  = None,
        location_id:    str  = None,   # FR16: routes the alert to assigned staff
        emotion:            str   = None,   # SER (§2.1.3) — vocal emotion, optional
        emotion_confidence: float = None,
        stt_confidence:     float = None,   # 0–1, Whisper transcription confidence
        nlp_confidence:     float = None,   # 0–1, raw NLP toxicity probability (pre-boost)
        scream_confidence:  float = None,   # 0–1, scream detection confidence
    ):
        sent = await self._broadcast({
            "type":           "ALERT",
            "alert_id":       alert_id,
            "event_id":       event_id,
            "location_id":    location_id,
            "location_name":  location_name,
            "severity":       severity,
            "threat_score":   threat_score,
            "classification": classification,
            "transcript":     transcript,
            "audio_url":      audio_url,
            "timestamp":      timestamp or datetime.utcnow().isoformat(),
            "emotion":            emotion,
            "emotion_confidence": emotion_confidence,
            "stt_confidence":     stt_confidence,
            "nlp_confidence":     nlp_confidence,
            "scream_confidence":  scream_confidence,
        }, location_id=location_id)
        logger.info(
            f"WS broadcast: ALERT severity={severity} location={location_id or 'ALL'} "
            f"sent to {sent}/{len(self.connections)} clients"
        )
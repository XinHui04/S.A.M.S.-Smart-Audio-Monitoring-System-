"""
services/websocket_manager.py
═══════════════════════════════════════════════════════
MODULE 4: Main Computer Monitoring System — real-time push
═══════════════════════════════════════════════════════

Manages all live WebSocket connections from the dashboard browser.
When the pipeline fires an alert, it calls broadcast_alert() here,
which instantly pushes it to every connected dashboard tab.

Dashboard connects to: ws://localhost:8000/ws/dashboard
"""
import json
import logging
from datetime import datetime
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class WebSocketManager:

    def __init__(self):
        self.connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.add(websocket)
        logger.info(f"Dashboard connected. Active: {len(self.connections)}")
        await websocket.send_json({
            "type":      "CONNECTED",
            "message":   "Connected to S.A.M.S. live alert feed",
            "timestamp": datetime.utcnow().isoformat(),
        })

    def disconnect(self, websocket: WebSocket):
        self.connections.discard(websocket)
        logger.info(f"Dashboard disconnected. Active: {len(self.connections)}")

    async def _broadcast(self, message: dict):
        dead = set()
        for ws in self.connections:
            try:
                await ws.send_text(json.dumps(message, default=str))
            except (WebSocketDisconnect, Exception):
                dead.add(ws)
        self.connections -= dead

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
    ):
        await self._broadcast({
            "type":           "ALERT",
            "alert_id":       alert_id,
            "event_id":       event_id,
            "location_name":  location_name,
            "severity":       severity,
            "threat_score":   threat_score,
            "classification": classification,
            "transcript":     transcript,
            "audio_url":      audio_url,
            "timestamp":      timestamp or datetime.utcnow().isoformat(),
        })
        logger.info(f"WS broadcast: ALERT severity={severity} to {len(self.connections)} clients")

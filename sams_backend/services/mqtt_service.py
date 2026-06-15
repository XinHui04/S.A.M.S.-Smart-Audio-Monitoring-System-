"""
services/mqtt_service.py
═══════════════════════════════════════════════════════
MODULE 4: Main Computer Monitoring System — MQTT fan-out
═══════════════════════════════════════════════════════

Publishes alert events to an MQTT topic (default: sams/alerts) so external
subscribers (mobile apps, other dashboards, sirens) receive the same alert
that the WebSocket pushes to the browser. Matches Figs 4.1/4.2 of the report.

Opt-in: if `enabled` is False (the default), this is a no-op. If the broker
is unreachable it logs a warning and the pipeline continues unaffected.
"""
import json
import logging

logger = logging.getLogger(__name__)


class MqttService:

    def __init__(
        self,
        enabled:  bool = False,
        host:     str  = "localhost",
        port:     int  = 1883,
        topic:    str  = "sams/alerts",
        username: str  = "",
        password: str  = "",
        use_tls:  bool = False,
        qos:      int  = 1,
    ):
        self.enabled  = enabled
        self.host     = host
        self.port     = port
        self.topic    = topic
        self.username = username
        self.password = password
        self.use_tls  = use_tls
        self.qos      = qos
        self._client    = None
        self._connected = False

    def connect(self) -> None:
        """Start a background MQTT connection. Best-effort; never raises."""
        if not self.enabled:
            logger.info("MQTT disabled — alert fan-out over MQTT is off.")
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning("paho-mqtt not installed — MQTT publishing disabled.")
            self.enabled = False
            return
        try:
            client = mqtt.Client()
            if self.username:
                client.username_pw_set(self.username, self.password)
            if self.use_tls:
                client.tls_set()
            client.on_connect    = self._on_connect
            client.on_disconnect = self._on_disconnect
            # connect_async + loop_start => non-blocking startup + auto-reconnect
            client.connect_async(self.host, self.port, keepalive=60)
            client.loop_start()
            self._client = client
            logger.info(f"MQTT connecting to {self.host}:{self.port} (topic '{self.topic}')")
        except Exception as e:
            logger.warning(f"MQTT connect failed ({e}) — publishing disabled.")
            self._client = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            logger.info(f"MQTT connected to {self.host}:{self.port}")
        else:
            self._connected = False
            logger.warning(f"MQTT connection refused (rc={rc}).")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        logger.info(f"MQTT disconnected (rc={rc}).")

    def publish_alert(self, **payload) -> None:
        """
        Publish an alert payload (same shape as the WebSocket broadcast) as JSON
        to the configured topic. Best-effort; never raises.
        """
        if not self.enabled or self._client is None:
            return
        try:
            message = json.dumps({"type": "ALERT", **payload}, default=str)
            self._client.publish(self.topic, message, qos=self.qos, retain=False)
            logger.info(f"MQTT published ALERT to '{self.topic}' (qos={self.qos})")
        except Exception as e:
            logger.warning(f"MQTT publish failed ({e}) — alert not published over MQTT.")

    def disconnect(self) -> None:
        """Stop the network loop and disconnect cleanly. Best-effort."""
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("MQTT client stopped.")
        except Exception as e:
            logger.warning(f"MQTT disconnect error ({e}).")
        finally:
            self._client = None
            self._connected = False
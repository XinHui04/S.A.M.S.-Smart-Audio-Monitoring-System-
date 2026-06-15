"""
tests/test_mqtt_service.py
Unit tests for the MQTT alert fan-out service.

These tests use unittest.mock — no real broker and no network are involved.

Run with:
    pytest tests/ -v
"""
import json
from unittest.mock import Mock

from services.mqtt_service import MqttService


def test_publish_when_disabled_is_noop():
    """enabled=False => publish_alert must not touch the client at all."""
    service = MqttService(enabled=False)
    service._client = Mock()

    result = service.publish_alert(alert_id="a1")

    assert result is None
    service._client.publish.assert_not_called()


def test_publish_with_no_client_is_noop():
    """enabled=True but no client connected => no-op, must not raise."""
    service = MqttService(enabled=True)
    # _client is None by default (connect() was never called)
    assert service._client is None

    result = service.publish_alert(alert_id="a1")

    assert result is None


def test_publish_alert_sends_json_to_topic():
    """A normal publish serialises the payload to JSON and forwards it."""
    service = MqttService(enabled=True, topic="sams/alerts", qos=1)
    service._client = Mock()

    service.publish_alert(
        alert_id="a1",
        event_id="e1",
        location_name="Toilet Block A",
        severity="high",
        threat_score=0.91,
        classification="threat",
        transcript="kill you",
        audio_url="/api/events/e1/audio",
        timestamp="2026-06-15T10:00:00",
    )

    service._client.publish.assert_called_once()
    call = service._client.publish.call_args

    # First positional arg is the topic.
    assert call.args[0] == "sams/alerts"

    # Second positional arg is the JSON message.
    payload = json.loads(call.args[1])
    assert payload["type"] == "ALERT"
    assert payload["alert_id"] == "a1"
    assert payload["severity"] == "high"
    assert payload["classification"] == "threat"
    assert payload["threat_score"] == 0.91

    # Keyword args control QoS and retain flag.
    assert call.kwargs["qos"] == 1
    assert call.kwargs["retain"] is False


def test_publish_swallows_client_exception():
    """A broker/client failure during publish must never propagate."""
    service = MqttService(enabled=True)
    service._client = Mock()
    service._client.publish.side_effect = Exception("broker gone")

    # Should not raise.
    result = service.publish_alert(alert_id="a1")

    assert result is None


def test_disconnect_stops_and_clears_client():
    """disconnect() stops the loop, disconnects, and clears the client ref."""
    service = MqttService(enabled=True)
    client = Mock()
    service._client = client

    service.disconnect()

    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()
    assert service._client is None
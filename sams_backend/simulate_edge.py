r"""
simulate_edge.py — Edge Device Simulator (stands in for the ESP32-C3 + INMP441)
================================================================================
The edge device's only job is to POST a captured audio clip + metadata to the
cloud backend. While the hardware is still being built, this script sends that
exact request so the full cloud pipeline (Module 1-4) can be demonstrated.

Usage (from the sams_backend folder, with the venv Python):
    .\.venv\Scripts\python.exe simulate_edge.py
    .\.venv\Scripts\python.exe simulate_edge.py --clip scream_clip.wav --device esp32-003 --location loc-003 --intensity 115 --pitch 2400
"""
import argparse
import os
from datetime import datetime

import requests

DEFAULT_URL = "http://127.0.0.1:8000/api/events/audio"


def main():
    here = os.path.dirname(os.path.abspath(__file__))

    ap = argparse.ArgumentParser(description="Simulate an ESP32 edge device submitting an audio event.")
    ap.add_argument("--clip", default="test_clip.wav", help="WAV file to send (relative to this folder or absolute)")
    ap.add_argument("--device", default="esp32-001", help="Device ID (e.g. esp32-001)")
    ap.add_argument("--location", default="loc-001", help="Location ID (e.g. loc-001)")
    ap.add_argument("--intensity", type=float, default=88.5, help="Sound intensity in dB")
    ap.add_argument("--pitch", type=float, default=320.0, help="Dominant pitch in Hz")
    ap.add_argument("--confidence", type=float, default=0.9, help="Edge scream-classifier confidence 0-1")
    ap.add_argument("--url", default=DEFAULT_URL, help="Backend endpoint URL")
    args = ap.parse_args()

    clip_path = args.clip if os.path.isabs(args.clip) else os.path.join(here, args.clip)
    if not os.path.exists(clip_path):
        raise SystemExit(f"Audio clip not found: {clip_path}")

    fields = {
        "device_id":        args.device,
        "location_id":      args.location,
        "timestamp":        datetime.now().replace(microsecond=0).isoformat(),
        "intensity":        str(args.intensity),
        "pitch":            str(args.pitch),
        "confidence_score": str(args.confidence),
        "duration_seconds": "6.0",
    }

    print("=" * 60)
    print("  EDGE DEVICE SIMULATOR  (stand-in for ESP32-C3 + INMP441)")
    print("=" * 60)
    print(f"  device   : {args.device} @ {args.location}")
    print(f"  acoustic : {args.intensity} dB | {args.pitch} Hz | edge conf {args.confidence}")
    print(f"  clip     : {os.path.basename(clip_path)}")
    print(f"  POST     : {args.url}")
    print("-" * 60)

    with open(clip_path, "rb") as f:
        files = {"audio_file": (os.path.basename(clip_path), f, "audio/wav")}
        resp = requests.post(args.url, data=fields, files=files, timeout=120)

    resp.raise_for_status()
    r = resp.json()
    a = r.get("analysis", {})

    print(f"  transcript     : {r.get('transcript', {}).get('text', '')!r}")
    print(f"  threat score   : {a.get('threat_score')}")
    print(f"  severity       : {a.get('severity_level')}")
    print(f"  classification : {a.get('classification')}")
    print(f"  ALERT FIRED    : {r.get('alert_fired')}")
    print("=" * 60)
    if r.get("alert_fired"):
        print("  -> Pushed live to the dashboard (WebSocket) and MQTT 'sams/alerts'.")
    else:
        print("  -> Logged; below threat threshold, no alert raised.")


if __name__ == "__main__":
    main()

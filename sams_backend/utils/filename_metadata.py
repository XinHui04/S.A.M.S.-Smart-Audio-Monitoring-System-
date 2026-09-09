"""
utils/filename_metadata.py
Parses device metadata embedded directly in the WAV filename the ESP32
firmware uploads to Supabase Storage, e.g.:

    esp32-001_loc-003_3421_20260909T153045_a1b2.wav

Fields (underscore-separated, in this exact order):
    device_id   - must not itself contain "_" (hyphens are fine)
    location_id - must not itself contain "_"
    sound_level - integer or decimal ADC/dB reading
    timestamp   - compact local time, format YYYYMMDDTHHMMSS
    short_id    - 4-8 hex chars, random, guards same-second collisions

Replaces the old flow where the firmware uploaded a companion .json
metadata file before streaming the WAV — that extra PUT added ~3s of
latency per detection event. Old bare-UUID filenames (e.g.
"1ea33c87-....wav") simply won't match this pattern, so callers should
treat a None return as "fall back to legacy .json-lookup handling".
"""
import re
from datetime import datetime
from typing import Optional, TypedDict


class ClipMetadata(TypedDict):
    device_id:   str
    location_id: str
    sound_level: float
    timestamp:   datetime
    short_id:    str


_METADATA_FILENAME_RE = re.compile(
    r"^(?P<device_id>[A-Za-z0-9\-]+)_"
    r"(?P<location_id>[A-Za-z0-9\-]+)_"
    r"(?P<sound_level>\d+(?:\.\d+)?)_"
    r"(?P<timestamp>\d{8}T\d{6})_"
    r"(?P<short_id>[0-9a-fA-F]{4,8})"
    r"\.wav$",
    re.IGNORECASE,
)


def parse_metadata_filename(filename: str) -> Optional[ClipMetadata]:
    """
    Returns the parsed fields if `filename` matches the new
    metadata-in-filename convention, else None.
    """
    match = _METADATA_FILENAME_RE.match(filename)
    if not match:
        return None

    groups = match.groupdict()
    try:
        timestamp = datetime.strptime(groups["timestamp"], "%Y%m%dT%H%M%S")
    except ValueError:
        return None   # malformed timestamp — treat as not-a-match, fall back safely

    return {
        "device_id":   groups["device_id"],
        "location_id": groups["location_id"],
        "sound_level": float(groups["sound_level"]),
        "timestamp":   timestamp,
        "short_id":    groups["short_id"],
    }
"""
tests/test_stt_service.py
Unit tests for STTService._compute_confidence().

No network, no Groq client construction — _compute_confidence() is a pure
helper over Whisper segment data, so it's exercised directly.

Run with:
    pytest tests/ -v
"""
import math

from services.stt_service import STTService


def make_service():
    # No API key needed — _compute_confidence never touches the Groq client.
    return STTService(api_key="dummy")


def test_mean_exp_avg_logprob_from_dict_segments():
    svc = make_service()
    segments = [{"avg_logprob": 0.0}, {"avg_logprob": -1.0}]

    result = svc._compute_confidence(segments)

    expected = round((math.exp(0.0) + math.exp(-1.0)) / 2, 4)
    assert result == expected


def test_single_segment_dict():
    svc = make_service()
    segments = [{"avg_logprob": -0.5}]

    result = svc._compute_confidence(segments)

    assert result == round(math.exp(-0.5), 4)


def test_object_style_segments_with_getattr():
    """Some Groq SDK versions return segment objects instead of dicts."""
    class Seg:
        def __init__(self, avg_logprob):
            self.avg_logprob = avg_logprob

    svc = make_service()
    segments = [Seg(0.0), Seg(-2.0)]

    result = svc._compute_confidence(segments)

    expected = round((math.exp(0.0) + math.exp(-2.0)) / 2, 4)
    assert result == expected


def test_result_clamped_to_one():
    """A positive avg_logprob would push exp() above 1.0 — must clamp."""
    svc = make_service()
    segments = [{"avg_logprob": 1.0}]

    result = svc._compute_confidence(segments)

    assert result == 1.0


def test_empty_segments_returns_none():
    svc = make_service()
    assert svc._compute_confidence([]) is None


def test_none_segments_returns_none():
    svc = make_service()
    assert svc._compute_confidence(None) is None


def test_segments_missing_avg_logprob_returns_none():
    svc = make_service()
    segments = [{"text": "hello"}, {"text": "world"}]
    assert svc._compute_confidence(segments) is None


def test_partial_missing_avg_logprob_ignores_missing():
    """Segments without avg_logprob are skipped, not treated as 0."""
    svc = make_service()
    segments = [{"avg_logprob": -1.0}, {"text": "no logprob here"}]

    result = svc._compute_confidence(segments)

    assert result == round(math.exp(-1.0), 4)


def test_malformed_segment_shapes_do_not_raise():
    """Garbage segment shapes (ints, None, plain strings) must degrade to
    None rather than raising — confidence computation must never break
    transcription output."""
    svc = make_service()
    segments = [None, 42, "not-a-segment", object()]

    result = svc._compute_confidence(segments)

    assert result is None


def test_mixed_valid_and_malformed_segments_does_not_raise():
    """Malformed entries are skipped (getattr/get default to None); the one
    valid segment still contributes — no exception either way."""
    svc = make_service()
    segments = [{"avg_logprob": -1.0}, None, "garbage"]

    result = svc._compute_confidence(segments)

    assert result == round(math.exp(-1.0), 4)
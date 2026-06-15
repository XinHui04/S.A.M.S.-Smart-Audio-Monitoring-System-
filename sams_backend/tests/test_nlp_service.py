"""
tests/test_nlp_service.py
Unit tests for the NLP threat classifier.

Run with:
    pytest tests/ -v
"""
import pytest
import asyncio
from services.nlp_service import NLPThreatClassifier


@pytest.fixture
def classifier():
    # Use keyword-only mode for fast tests (no model download needed)
    clf = NLPThreatClassifier(threshold=0.75)
    # Monkey-patch _load_model to raise so we test keyword fallback path
    clf._pipeline = None
    return clf


@pytest.mark.asyncio
async def test_normal_conversation_low_score(classifier):
    result = await classifier.analyse("Hey, can you help me with the homework?")
    assert result.severity_level in ("low", "medium")
    assert result.classification in ("normal", "verbal_bullying")


@pytest.mark.asyncio
async def test_english_bullying_detected(classifier):
    result = await classifier.analyse("You are such a loser, nobody likes you, just go die!")
    assert result.threat_score > 0.0
    assert len(result.keywords_found) > 0


@pytest.mark.asyncio
async def test_malay_bullying_detected(classifier):
    result = await classifier.analyse("Bodoh lah kau, pergi mampus!")
    assert len(result.keywords_found) > 0


@pytest.mark.asyncio
async def test_manglish_detected(classifier):
    result = await classifier.analyse("You damn stupid la, mau kena ke?")
    assert len(result.keywords_found) > 0


@pytest.mark.asyncio
async def test_empty_transcript(classifier):
    result = await classifier.analyse("")
    assert result.classification == "normal"
    assert result.threat_score == 0.0


@pytest.mark.asyncio
async def test_severity_levels(classifier):
    """Test that severity maps correctly to score bands."""
    # Override threat_score directly by testing the _classify method
    clf = NLPThreatClassifier()

    _, sev_high   = clf._classify(0.90, ["go die"])
    _, sev_medium = clf._classify(0.65, [])
    _, sev_low    = clf._classify(0.30, [])

    assert sev_high   == "high"
    assert sev_medium == "medium"
    assert sev_low    == "low"

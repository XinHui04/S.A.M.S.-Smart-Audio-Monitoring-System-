"""
tests/test_nlp_service.py
Unit tests for the NLP threat classifier.

Run with:
    pytest tests/ -v
"""
import pytest
import asyncio
from services.nlp_service import NLPService


@pytest.fixture
def classifier():
    # Keyword-only mode for fast, offline tests: stub the transformer layer so
    # no model is downloaded — exercises the keyword + classification logic.
    clf = NLPService(threshold=0.75)
    clf._transformer_score = lambda text: (0.0, "model_unavailable")
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
    # Override threat_score directly by testing the _classify method.
    # _classify(base_score, keywords, text) -> (classification, severity, score)
    clf = NLPService()

    _, sev_high,   _ = clf._classify(0.90, ["go die"], "go die")
    _, sev_medium, _ = clf._classify(0.65, [], "ok")
    _, sev_low,    _ = clf._classify(0.30, [], "ok")

    assert sev_high   == "high"
    assert sev_medium == "medium"
    assert sev_low    == "low"

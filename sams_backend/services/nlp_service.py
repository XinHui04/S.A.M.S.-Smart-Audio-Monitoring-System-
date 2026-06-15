"""
services/nlp_service.py
═══════════════════════════════════════════════════════
MODULE 2: Cloud Processing & AI Analysis — Part B (NLP)
═══════════════════════════════════════════════════════

Takes the transcript from STT and classifies it for threat content.

Two-layer approach (Section 2.1.2):
  Layer 1 — XLM-RoBERTa transformer: understands semantic context,
             handles code-switching, detects subtle aggression
  Layer 2 — Keyword scan: catches known Malay/Manglish slang that
             the model may not have seen in training data

Output: ThreatResult with score (0–1), severity, classification
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Keyword layer: Malay + Manglish school bullying terms ────────────────────
# Supplements the transformer for local slang (Section 2.1.2 justification)
BULLYING_KEYWORDS = {
    # English
    "shut up", "loser", "nobody likes you", "go die", "kill yourself",
    "i'll hurt you", "beat you up", "you're dead", "freak", "idiot", "stupid",
    # Malay
    "bodoh", "babi", "sial", "celaka", "pukimak", "anak haram",
    "mati lah", "pergi mampus", "bangang", "diam", "lancau",
    # Manglish blends common in Malaysian schools
    "you damn stupid", "mau kena ke", "nak kena", "balik la bodoh",
    "you all bully", "kena belasah",
}


@dataclass
class ThreatResult:
    threat_score:   float
    severity_level: str        # low | medium | high
    classification: str        # verbal_bullying | threat | distress | normal
    keywords_found: list[str]  = field(default_factory=list)
    language:       str        = "unknown"


class NLPService:

    def __init__(
        self,
        model_name: str   = "cardiffnlp/twitter-xlm-roberta-base-offensive",
        threshold:  float = 0.75,
    ):
        self.model_name = model_name
        self.threshold  = threshold
        self._pipeline  = None   # lazy-loaded

    def _load(self):
        if self._pipeline is None:
            try:
                from transformers import pipeline
                logger.info(f"Loading NLP model: {self.model_name}")
                self._pipeline = pipeline(
                    "text-classification",
                    model            = self.model_name,
                    tokenizer        = self.model_name,
                    return_all_scores = True,
                    truncation       = True,
                    max_length       = 512,
                )
                logger.info("NLP model ready.")
            except ImportError:
                raise RuntimeError(
                    "transformers not installed. Run: pip install transformers torch"
                )
        return self._pipeline

    def _keyword_scan(self, text: str) -> list[str]:
        t = text.lower()
        return [kw for kw in BULLYING_KEYWORDS if kw in t]

    def _transformer_score(self, text: str) -> tuple[float, str]:
        """
        Runs XLM-RoBERTa and returns (offensive_score, raw_label).
        Falls back to 0.0 if model unavailable.
        """
        try:
            pipe   = self._load()
            output = pipe(text)
            # output: [[{"label": "LABEL_0/1", "score": float}, ...]]
            scores = output[0] if isinstance(output[0], list) else output
            label_map = {item["label"]: item["score"] for item in scores}

            # LABEL_1 = offensive in cardiffnlp model
            score = label_map.get("LABEL_1", label_map.get("offensive", 0.0))
            label = "offensive" if score >= 0.5 else "not_offensive"
            return float(score), label

        except Exception as e:
            logger.warning(f"Transformer inference failed ({e}) — keyword-only fallback.")
            return 0.0, "model_unavailable"

    def _classify(
        self, base_score: float, keywords: list[str], text: str
    ) -> tuple[str, str, float]:
        """
        Combines transformer score + keyword hits into final result.
        Returns (classification, severity, final_score).
        """
        # Small boost per keyword found — compensates for Manglish gaps
        boosted = min(1.0, base_score + 0.08 * len(keywords))

        # Severity bands
        if boosted >= 0.85:
            severity = "high"
        elif boosted >= 0.60:
            severity = "medium"
        else:
            severity = "low"

        # Classification type
        t = text.lower()
        threat_words   = ["kill", "hurt", "dead", "mati", "nak kena", "belasah"]
        distress_words = ["help", "stop", "please", "tolong", "jangan", "sakit"]

        if any(w in t for w in threat_words) and boosted > 0.5:
            classification = "threat"
        elif any(w in t for w in distress_words) and boosted > 0.4:
            classification = "distress"
        elif boosted >= self.threshold:
            classification = "verbal_bullying"
        else:
            classification = "normal"

        return classification, severity, round(boosted, 4)

    async def analyse(self, transcript_text: str, language: str = "unknown") -> ThreatResult:
        """
        Full NLP analysis pipeline.
        Input:  transcript text from STTService (Module 2 Part A)
        Output: ThreatResult with score, severity, classification
        """
        if not transcript_text or len(transcript_text.strip()) < 3:
            return ThreatResult(
                threat_score   = 0.0,
                severity_level = "low",
                classification = "normal",
                language       = language,
            )

        keywords               = self._keyword_scan(transcript_text)
        base_score, raw_label  = self._transformer_score(transcript_text)
        classification, severity, final_score = self._classify(
            base_score, keywords, transcript_text
        )

        logger.info(
            f"NLP result | score={final_score} | severity={severity} | "
            f"class={classification} | keywords={keywords}"
        )

        return ThreatResult(
            threat_score   = final_score,
            severity_level = severity,
            classification = classification,
            keywords_found = keywords,
            language       = language,
        )

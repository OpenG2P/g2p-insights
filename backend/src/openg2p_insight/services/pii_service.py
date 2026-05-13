"""
Local PII proxy.

Detects names, IDs, phones, emails, locations in the user's natural-language
query, replaces them with stable placeholders, and stores the reverse map so
the SQL planner can re-inject the real values at execution time.

Country-specific recognizers (e.g. Fayda token, Aadhaar) load from
catalog/pii_recognizers.yaml.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


@dataclass
class PiiRedaction:
    placeholder: str
    entity_type: str
    start: int
    end: int


@dataclass
class SanitizedQuery:
    text: str
    redactions: List[PiiRedaction] = field(default_factory=list)
    placeholder_map: Dict[str, str] = field(default_factory=dict)


class PiiService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._analyzer = None
        self._custom_patterns: List[dict] = []
        self._load_custom_patterns()
        self._init_analyzer()

    # ---------- public API -----------------------------------------------------

    def sanitize(self, text: str, session_id: Optional[str] = None) -> SanitizedQuery:
        """
        Detect PII spans, replace each with a stable placeholder, and return a
        SanitizedQuery containing the safe text + reverse map.

        Overlapping or nested NER spans (Presidio often returns both 'John' and
        'John Mayer') are merged first so the substitution loop never reads
        backwards over an already-replaced region.
        """
        spans = self._detect(text)
        if not spans:
            return SanitizedQuery(text=text)

        spans = self._merge_overlapping(spans)

        counters: Dict[str, int] = defaultdict(int)
        placeholder_map: Dict[str, str] = {}
        redactions: List[PiiRedaction] = []

        new_parts: List[str] = []
        cursor = 0
        for span in spans:
            # _merge_overlapping guarantees non-overlap and ascending order.
            counters[span["type"]] += 1
            placeholder = f"[{span['type']}_{counters[span['type']]}]"
            placeholder_map[placeholder] = text[span["start"] : span["end"]]
            new_parts.append(text[cursor : span["start"]])
            new_parts.append(placeholder)
            cursor = span["end"]
            redactions.append(
                PiiRedaction(
                    placeholder=placeholder,
                    entity_type=span["type"],
                    start=span["start"],
                    end=span["end"],
                )
            )
        new_parts.append(text[cursor:])

        return SanitizedQuery(
            text="".join(new_parts), redactions=redactions, placeholder_map=placeholder_map
        )

    @staticmethod
    def _merge_overlapping(spans: List[dict]) -> List[dict]:
        """Merge overlapping / contiguous spans, preferring the longest. When
        two spans overlap and have different entity types, we keep the longer
        one — Presidio's PERSON entities for 'John Mayer' subsume isolated
        'John' detections from less specific recognisers."""
        if not spans:
            return spans
        sorted_spans = sorted(spans, key=lambda s: (s["start"], -(s["end"] - s["start"])))
        merged: List[dict] = []
        for s in sorted_spans:
            if not merged or s["start"] >= merged[-1]["end"]:
                merged.append(dict(s))
                continue
            # Overlap: keep whichever covers more characters.
            current = merged[-1]
            if (s["end"] - s["start"]) > (current["end"] - current["start"]):
                merged[-1] = dict(s)
        return merged

    # ---------- internals ------------------------------------------------------

    def _init_analyzer(self) -> None:
        """Lazily build the Presidio AnalyzerEngine. Falls back to regex-only mode
        if Presidio/spaCy aren't available (used during early scaffolding and tests)."""
        try:
            from presidio_analyzer import AnalyzerEngine, PatternRecognizer
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": _config.pii.spacy_model}],
                }
            )
            engine = AnalyzerEngine(nlp_engine=provider.create_engine())

            for pattern in self._custom_patterns:
                engine.registry.add_recognizer(
                    PatternRecognizer(
                        supported_entity=pattern["entity"],
                        patterns=pattern["patterns"],
                        context=pattern.get("context", []),
                    )
                )
            self._analyzer = engine
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Presidio init failed (%s); using regex-only mode", exc)
            self._analyzer = None

    def _load_custom_patterns(self) -> None:
        path = Path(_config.catalog_path) / "pii_recognizers.yaml"
        if not path.exists():
            return
        try:
            data = yaml.safe_load(path.read_text()) or {}
            self._custom_patterns = data.get("recognizers", [])
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Could not load custom PII patterns from %s: %s", path, exc)

    def _detect(self, text: str) -> List[dict]:
        if self._analyzer is None:
            return self._regex_fallback(text)
        results = self._analyzer.analyze(
            text=text, language="en", entities=_config.pii.enabled_recognizers
        )
        return [{"type": r.entity_type, "start": r.start, "end": r.end} for r in results]

    @staticmethod
    def _regex_fallback(text: str) -> List[dict]:
        """Minimal regex matcher used when Presidio isn't initialized — never used in prod."""
        spans: List[dict] = []
        for m in re.finditer(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", text):
            spans.append({"type": "PERSON", "start": m.start(), "end": m.end()})
        for m in re.finditer(r"\+?\d[\d\s\-]{7,}\d", text):
            spans.append({"type": "PHONE_NUMBER", "start": m.start(), "end": m.end()})
        return spans

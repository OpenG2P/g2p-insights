"""PII-service tests — covers the merging fix and the regex fallback."""

from openg2p_insight.services.pii_service import PiiService


def _bare_svc() -> PiiService:
    svc = PiiService.__new__(PiiService)
    svc._analyzer = None
    svc._custom_patterns = []
    return svc


def test_regex_fallback_replaces_person_names():
    svc = _bare_svc()
    out = svc.sanitize("Income of John Mayer in 2024")
    assert "[PERSON_1]" in out.text
    assert "John" not in out.text
    assert out.placeholder_map["[PERSON_1]"] == "John Mayer"


def test_overlapping_spans_are_merged_longest_wins():
    """Two PERSON spans both starting at position N — the longer must win and
    the shorter must NOT remain in the text. Without merging, the cursor loop
    would emit `[PERSON_1][PERSON_2] Mayer` and corrupt the query."""
    svc = _bare_svc()
    text = "Income of John Mayer here"
    spans = [
        {"type": "PERSON", "start": 10, "end": 14},   # "John"
        {"type": "PERSON", "start": 10, "end": 20},   # "John Mayer"
    ]
    merged = svc._merge_overlapping(spans)
    assert merged == [{"type": "PERSON", "start": 10, "end": 20}]
    # And the sanitised output is well-formed.
    monkey = svc._detect  # silence unused-import
    svc._detect = lambda _t: spans  # type: ignore[assignment]
    try:
        out = svc.sanitize(text)
        assert out.text == "Income of [PERSON_1] here"
        assert out.placeholder_map["[PERSON_1]"] == "John Mayer"
    finally:
        svc._detect = monkey  # type: ignore[assignment]

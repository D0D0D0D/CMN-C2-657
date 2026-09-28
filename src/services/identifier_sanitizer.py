"""AgentCore Platform v1.0 — CMN-C2-657 identifier sanitizer (resume-path defense).

WHY THIS EXISTS: the HITL resume payload (recruiter-authored revision/approval
text) enters the graph via ``agent.resume()`` — it does NOT pass through
pre_process, and the framework persists it as ``hitl_feedback`` in checkpoints.
Without adapter-side sanitization the docs/02 §5-0 invariant ("no direct
identifier / prohibited-attribute lexeme in ANY checkpoint") breaks on the
resume path even though the initial /invoke input is sanitized.

KNOWN GAP in ``shared.security.pii_detector.detect_pii`` (confirmed on the live
1.0.0 wheel by sibling template CorporateGroupTripPlannerAgent during its integration testing):
  1. No Japanese personal-name detection — the ``name`` pattern is a
     Latin-script Title-Case heuristic and never matches e.g. "田中太郎".
  2. Patterns ending in a trailing ``\\b`` (``email``, ``phone_jp``,
     ``phone_us``) silently fail when the identifier is immediately followed
     by Japanese text with no delimiter — e.g. "080-1234-5678です" or
     "yamada@example.comにも共有". Unicode-aware ``\\b`` sees the trailing
     ASCII word char and the following Japanese char (also a word char) as
     adjacent, so no boundary exists and the whole match fails.

``shared.security.pii_detector`` is a framework-provided module and MUST NOT be
edited — this module adds a supplementary regex pass AFTER detect_pii(),
narrowly scoped (honorific-suffixed names; boundary-free email/phone shapes)
to avoid over-redacting legitimate revision text.
"""

from __future__ import annotations

import re
from typing import Any

from shared.security.pii_detector import detect_pii

from src.services.appi_dictionary import mask_sensitive

# Lexeme-free marker: must not contain any SENSITIVE_CATEGORIES lexeme or
# category code (e.g. "age"), or downstream detection would re-trigger on it.
_REDACTION_MARK = "[識別子除去済み]"

_SUPPLEMENTARY_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # Email without the failing trailing \b (fixes email-followed-by-Japanese).
    ("email_suppl", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    # JP phone without the failing trailing \b.
    ("phone_jp_suppl", re.compile(r"(?<!\d)(?:\+81[-\s]?\d{1,4}[-\s]\d{1,4}[-\s]\d{4}|0\d{1,4}[-\s]\d{1,4}[-\s]\d{4})")),
    # Japanese personal name with an honorific suffix (high precision — the
    # honorific marks it as a person, not a place/noun): "田中太郎さん", "山田様".
    ("name_jp_honorific", re.compile(r"[一-鿿぀-ゟ゠-ヿ]{2,10}(?:さん|様|氏|くん|ちゃん)")),
]


def sanitize_identifiers(text: str) -> tuple[str, int]:
    """Strip direct identifiers from reviewer-authored free text.

    Returns ``(sanitized_text, redaction_count)``. Never returns or logs the
    removed spans/content — only the count (S-4 event payload contract).

    Runs detect_pii() first, THEN the supplementary pass to close the
    Japanese-name / trailing-boundary gaps documented above.
    """
    if not text:
        return "", 0

    spans: list[tuple[int, int]] = [(f["start"], f["end"]) for f in detect_pii(text)]
    for _label, pattern in _SUPPLEMENTARY_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))

    if not spans:
        return text, 0

    # Merge overlapping/adjacent spans so double matches don't corrupt offsets.
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    sanitized = text
    for start, end in reversed(merged):  # reverse to preserve offsets
        sanitized = sanitized[:start] + _REDACTION_MARK + sanitized[end:]
    return sanitized, len(merged)


def sanitize_resume_feedback(feedback: Any) -> tuple[Any, dict]:
    """Sanitize a HITL resume payload BEFORE it enters ``agent.resume()``.

    Recursively walks str/dict/list values; every string is passed through
    (a) APPI sensitive-lexeme masking (mask, not field-drop — dropping e.g.
    ``directive`` would destroy the human's revision instruction and break the
    HITL loop; masking preserves the instruction minus the lexemes) and
    (b) direct-identifier sanitization (detect_pii + JP supplementary pass).

    Returns ``(sanitized_feedback, stats)`` where stats carries ONLY counts /
    category codes: ``{"identifier_redactions": int, "sensitive_categories": [str]}``.
    """
    stats = {"identifier_redactions": 0, "sensitive_categories": set()}

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            masked, cats = mask_sensitive(value)
            stats["sensitive_categories"].update(cats)
            cleaned, n = sanitize_identifiers(masked)
            stats["identifier_redactions"] += n
            return cleaned
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    sanitized = _walk(feedback)
    return sanitized, {
        "identifier_redactions": stats["identifier_redactions"],
        "sensitive_categories": sorted(stats["sensitive_categories"]),
    }

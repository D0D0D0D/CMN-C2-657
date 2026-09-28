"""S-3 output guards (docs/02_design.md §5-3 / §2-5 non-assertive contract).

Three responsibilities, applied by ReportComposeNode._extra_security_gate_output():
  1. Assertive-phrase DOWNGRADE — decisive hire/pass verdicts are reframed to a
     "requires further confirmation" wording (downgrade, not delete = transparency).
  2. Prohibited-attribute mention scan — findings/questions that use a
     protected/prohibited attribute as an evaluation basis are removed (§4-3-4).
  3. Structural non-assertive contract — reject any output that reintroduces a
     pass/fail, recommendation, or overall/composite score field (§2-5).

Matching is NFKC-normalized + casefold to defend against fullwidth/case bypass.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from src.services.appi_dictionary import detect_sensitive_categories

# Decisive verdict phrases that must be downgraded (§5-3-1). JP + EN.
_ASSERTIVE_PATTERNS: list[str] = [
    "採用すべき", "不採用", "採用推奨", "不採用推奨", "合格", "不合格", "採用可", "採用不可",
    "採否は", "採用する", "採用しない", "落とすべき", "見送るべき",
    "should hire", "should not hire", "must hire", "do not hire", "reject the candidate",
    "recommend hiring", "not recommend", "pass/fail", "passed", "failed", "hire this candidate",
]

# Framing appended when a downgrade occurs.
_DOWNGRADE_SUFFIX = "（要確認: 本所見は人間の面接・審査で確認が必要なドラフトです / requires further human confirmation）"

# Fields that MUST NOT exist in the output schema (§2-5 structural guard).
FORBIDDEN_OUTPUT_KEYS: frozenset[str] = frozenset(
    {
        "recommendation", "recommend", "pass_fail", "passfail", "pass", "fail",
        "hire", "hire_decision", "decision", "verdict",
        "overall_score", "composite_score", "total_score", "score", "ranking", "rank",
    }
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def downgrade_assertive(text: str) -> tuple[str, bool]:
    """Downgrade decisive verdict phrasing in ``text`` to a confirmation framing.

    Returns ``(text, downgraded)``. The offending phrase is preserved but the
    confirmation suffix is appended once (transparency: we mark it, not hide it).
    """
    if not text or not isinstance(text, str):
        return text, False
    norm = _normalize(text)
    if any(_normalize(p) in norm for p in _ASSERTIVE_PATTERNS):
        if _DOWNGRADE_SUFFIX not in text:
            return text + " " + _DOWNGRADE_SUFFIX, True
        return text, True
    return text, False


def _walk_strings(obj: Any):
    """Yield (container, key_or_index, value) for every string leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                yield obj, k, v
            else:
                yield from _walk_strings(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                yield obj, i, v
            else:
                yield from _walk_strings(v)


def scan_forbidden_keys(report: dict) -> list[str]:
    """Return forbidden output keys found anywhere in the report (§2-5 structural)."""
    found: list[str] = []

    def _recurse(obj: Any):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and k.strip().casefold() in FORBIDDEN_OUTPUT_KEYS:
                    found.append(k)
                _recurse(v)
        elif isinstance(obj, list):
            for v in obj:
                _recurse(v)

    _recurse(report)
    return sorted(set(found))


def apply_output_guards(report: dict) -> tuple[dict, list[str]]:
    """Apply the three S-3 guards to a report dict in place-safe manner.

    Returns ``(report, degraded_flags)``.

    Raises RuntimeError if a forbidden pass/fail/score key is present — the
    non-assertive contract is structural and must never be silently repaired.
    """
    degraded: list[str] = []

    forbidden = scan_forbidden_keys(report)
    if forbidden:
        raise RuntimeError(
            f"S-3 non-assertive contract violation: forbidden output key(s) {forbidden} "
            "present (recommendation/pass_fail/overall_score are structurally prohibited, docs/02 §2-5)."
        )

    # Downgrade assertive phrasing + strip prohibited-attribute-based findings.
    for container, key, value in _walk_strings(report):
        new_value, downgraded = downgrade_assertive(value)
        if downgraded:
            degraded.append("assertive_downgraded")
        # Prohibited-attribute mention as evaluation basis -> redact the string.
        cats = detect_sensitive_categories(new_value)
        if cats:
            new_value = "[REDACTED: prohibited attribute reference removed]"
            degraded.append(f"prohibited_attribute_removed:{'|'.join(cats)}")
        if new_value != value:
            container[key] = new_value

    return report, sorted(set(degraded))

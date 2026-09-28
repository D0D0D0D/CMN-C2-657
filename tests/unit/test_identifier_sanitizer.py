"""Identifier sanitizer tests — HITL resume-path defense (docs/02 §5-0).

Covers the two known detect_pii gaps (confirmed on the live 1.0.0 wheel by
sibling template CorporateGroupTripPlannerAgent): Japanese personal names, and email/phone
immediately followed by Japanese text (trailing-\\b failure).
"""

from src.services.identifier_sanitizer import sanitize_identifiers, sanitize_resume_feedback


def test_japanese_name_with_honorific_redacted():
    text, n = sanitize_identifiers("田中太郎さんの評価を修正して")
    assert "田中太郎" not in text
    assert n == 1


def test_japanese_name_sama_redacted():
    text, n = sanitize_identifiers("山田様の件")
    assert "山田" not in text
    assert n == 1


def test_email_followed_by_japanese_redacted():
    """detect_pii's trailing \\b fails on 'email直後に日本語' — supplementary pass must catch it."""
    text, n = sanitize_identifiers("yamada@example.comにも共有して")
    assert "yamada@example.com" not in text
    assert n >= 1


def test_phone_jp_followed_by_japanese_redacted():
    text, n = sanitize_identifiers("080-1234-5678です")
    assert "080-1234-5678" not in text
    assert n >= 1


def test_latin_name_still_redacted():
    text, n = sanitize_identifiers("Please reflect John Smith's answer")
    assert "John Smith" not in text
    assert n >= 1


def test_clean_text_untouched():
    text, n = sanitize_identifiers("設問を技術面に寄せて修正してください")
    assert text == "設問を技術面に寄せて修正してください"
    assert n == 0


def test_resume_feedback_structure_preserved():
    """action/directive dict structure survives; only string contents are cleaned."""
    feedback = {"action": "correct", "directive": "田中様の年齢を考慮して修正", "count": 2}
    clean, stats = sanitize_resume_feedback(feedback)
    assert clean["action"] == "correct"          # action verb intact (HITL routing contract)
    assert clean["count"] == 2                    # non-strings untouched
    assert "田中" not in clean["directive"]
    assert "年齢" not in clean["directive"]       # sensitive lexeme masked
    assert stats["identifier_redactions"] >= 1
    assert "age" in stats["sensitive_categories"]


def test_resume_feedback_stats_carry_no_content():
    _, stats = sanitize_resume_feedback({"directive": "佐藤さん 090-1111-2222です"})
    blob = str(stats)
    assert "佐藤" not in blob and "090-1111-2222" not in blob  # counts/codes only


def test_marker_does_not_retrigger_detection():
    """The masking marker must not itself contain a detection lexeme (e.g. 'age')."""
    from src.services.appi_dictionary import detect_sensitive_categories, mask_sensitive
    masked, cats = mask_sensitive("年齢を考慮")
    assert cats == ["age"]
    assert detect_sensitive_categories(masked) == []

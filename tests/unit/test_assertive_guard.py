"""S-3 non-assertive contract guards (docs/02 §2-5 / §5-3)."""

import pytest

from src.services.assertive_guard import (
    FORBIDDEN_OUTPUT_KEYS,
    apply_output_guards,
    downgrade_assertive,
    scan_forbidden_keys,
)


def test_assertive_phrase_downgraded_not_deleted():
    text, downgraded = downgrade_assertive("この候補者は採用すべき")
    assert downgraded is True
    assert "採用すべき" in text  # preserved (transparency)
    assert "要確認" in text or "confirmation" in text.lower()


def test_english_assertive_downgraded():
    text, downgraded = downgrade_assertive("We should hire this candidate")
    assert downgraded is True


def test_fullwidth_assertive_downgraded():
    text, downgraded = downgrade_assertive("合格")  # decisive verdict
    assert downgraded is True


def test_non_assertive_text_untouched():
    text, downgraded = downgrade_assertive("This axis shows medium sufficiency with evidence")
    assert downgraded is False
    assert text == "This axis shows medium sufficiency with evidence"


@pytest.mark.parametrize("bad_key", sorted(FORBIDDEN_OUTPUT_KEYS))
def test_every_forbidden_key_detected(bad_key):
    assert scan_forbidden_keys({bad_key: "x"}) == [bad_key]


def test_forbidden_key_raises():
    with pytest.raises(RuntimeError):
        apply_output_guards({"potential_assessment": [{"overall_score": 90}]})


def test_prohibited_attribute_mention_redacted():
    report = {"fit_analysis": [{"note": "candidate's 国籍 was a factor"}]}
    guarded, degraded = apply_output_guards(report)
    assert "国籍" not in guarded["fit_analysis"][0]["note"]
    assert any("prohibited_attribute_removed" in d for d in degraded)


def test_clean_report_passes():
    report = {
        "potential_assessment": [{"axis": "skill", "sufficiency": "medium",
                                  "evidence": [{"source": "resume", "quote": "led team"}]}],
    }
    guarded, degraded = apply_output_guards(report)
    assert guarded["potential_assessment"][0]["sufficiency"] == "medium"
    assert degraded == []

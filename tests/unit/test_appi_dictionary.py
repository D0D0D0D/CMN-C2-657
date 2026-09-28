"""OI-1 (docs/02 §13): per-category APPI sensitive-attribute detection tests.

MANDATORY: one dedicated test per APPI special-care category + fair-hiring
prohibited attribute (docs/02 §4-2). This is the load-bearing OI-1 requirement.
"""

import pytest

from src.services.appi_dictionary import (
    SENSITIVE_CATEGORIES,
    contains_sensitive,
    detect_sensitive_categories,
)

# One representative lexeme per category for the dedicated per-category test.
_CATEGORY_SAMPLES = {
    "race": "候補者の人種について",
    "creed": "宗教は仏教です",
    "social_status": "社会的身分に関する記載",
    "medical_history": "病歴あり",
    "disability": "身体障害の記録",
    "health_checkup": "健康診断の結果",
    "medical_treatment": "診療録を添付",
    "criminal_record": "犯罪歴なし",
    "crime_victim": "犯罪被害の経験",
    "domicile_birthplace": "本籍地は東京",
    "family": "家族構成は4人",
    "labor_union": "労働組合に加入",
    "marriage_childbirth": "出産予定あり",
    "nationality": "国籍は日本",
    "age": "生年月日は非公開",
    "gender": "性別: 男性",
}


@pytest.mark.parametrize("category,text", list(_CATEGORY_SAMPLES.items()))
def test_each_appi_category_is_detected(category, text):
    """OI-1: every APPI special-care / prohibited category is detected + reported."""
    cats = detect_sensitive_categories(text)
    assert category in cats, f"category '{category}' not detected in {text!r}"


def test_all_categories_have_coverage():
    """Every declared category must have at least one lexeme that self-detects."""
    for cat, lexemes in SENSITIVE_CATEGORIES.items():
        assert detect_sensitive_categories(lexemes[0]), f"{cat} lexeme[0] does not self-detect"


def test_fullwidth_bypass_defended():
    """NFKC normalization defeats fullwidth bypass (docs/02 §4-2 banned-word guard)."""
    assert contains_sensitive("ＲＡＣＥ")  # fullwidth 'RACE'
    assert contains_sensitive("ＮＡＴＩＯＮＡＬＩＴＹ")


def test_case_bypass_defended():
    """casefold defeats case-variant bypass."""
    assert contains_sensitive("CRIMINAL RECORD")
    assert contains_sensitive("Disability")


def test_clean_text_no_false_positive():
    """Job-related text with no sensitive lexeme returns empty."""
    assert detect_sensitive_categories("Led the backend team and shipped the payment service") == []


def test_empty_text():
    assert detect_sensitive_categories("") == []
    assert not contains_sensitive("")

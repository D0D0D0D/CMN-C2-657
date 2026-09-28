"""Allowlist projection tests (docs/02 §4-2 / §4-3 — primary structural defense)."""

from src.services.allowlist import ALLOWED_FIELDS, project


def test_prohibited_attributes_structurally_excluded():
    """nationality/gender/age direct fields never survive the projection."""
    proj, warnings = project({
        "job_category": "engineer", "nationality": "JP", "gender": "male",
        "age": 34, "job_experience_text": "built systems",
    })
    assert "nationality" not in proj
    assert "gender" not in proj
    assert "age" not in proj
    assert any("dropped_field:nationality" in w for w in warnings)


def test_graduation_year_converted_to_experience_years():
    """Proxy variable: graduation_year -> experience_years (absolute date destroyed)."""
    proj, warnings = project({"job_category": "eng", "graduation_year": 2018})
    assert "graduation_year" not in proj
    assert isinstance(proj.get("experience_years"), int)
    assert any("dropped_proxy:graduation_year" in w for w in warnings)


def test_explicit_experience_years_preferred():
    proj, _ = project({"experience_years": 7, "graduation_year": 2010})
    assert proj["experience_years"] == 7


def test_sensitive_content_in_allowlisted_field_dropped():
    """Even an allowlisted free-text field is dropped if it carries a sensitive lexeme."""
    proj, warnings = project({"job_experience_text": "has 病歴 of chronic illness"})
    assert "job_experience_text" not in proj
    assert any("dropped_sensitive" in w for w in warnings)


def test_content_never_recorded_in_warnings():
    """Warnings record only field/category codes, never the offending content."""
    _, warnings = project({"job_experience_text": "本籍地は東京都渋谷区1-2-3"})
    joined = " ".join(warnings)
    assert "東京" not in joined
    assert "渋谷" not in joined


def test_only_allowlisted_fields_pass():
    proj, _ = project({k: "x" for k in ALLOWED_FIELDS} | {"salary_expectation": "high"})
    assert "salary_expectation" not in proj


def test_non_dict_payload():
    proj, warnings = project("not a dict")  # type: ignore[arg-type]
    assert proj == {}
    assert warnings

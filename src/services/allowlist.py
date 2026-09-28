"""Allowlist projection — the primary structural defense (docs/02_design.md §4-2/§4-3).

Rather than detecting-and-removing prohibited attributes at output time, this
service passes through ONLY an explicit allowlist of job-related fields at the
input boundary. Prohibited attributes and known proxy variables are structurally
excluded because they are not in the allowlist — they can never reach the
evaluation LLM prompt.

Proxy-variable handling (§4-3-2): date-of-birth / graduation_year encode age, so
they are converted to a bare numeric ``experience_years`` and the raw date fields
are dropped. Name (proxy for nationality/gender) is tokenized upstream in the API
adapter and is not in the allowlist here either.
"""

from __future__ import annotations

import datetime
from typing import Any

from src.services.appi_dictionary import detect_sensitive_categories

# Explicit allowlist of fields that may enter the evaluation pipeline (§4-2 (a)).
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "job_category",
        "phase",
        "experience_years",
        "skill_tags",
        "job_experience_text",   # free-text job/experience description
        "interview_response_text",  # free-text interview answer
        "evaluation_axis_code",
    }
)

# Fields that are proxy variables — converted, never passed through raw (§4-3-2).
_PROXY_DATE_FIELDS = ("graduation_year", "birth_year", "date_of_birth", "dob")


def _to_experience_years(payload: dict) -> int | None:
    """Derive experience_years from proxy date fields WITHOUT retaining the date.

    If experience_years is already provided, prefer it. Otherwise derive a coarse
    value from graduation_year (years since graduation) — this deliberately loses
    the absolute date so age cannot be reconstructed.
    """
    exp = payload.get("experience_years")
    if isinstance(exp, (int, float)) and exp >= 0:
        return int(exp)
    grad = payload.get("graduation_year")
    try:
        grad_year = int(grad)
        current_year = datetime.datetime.now(datetime.timezone.utc).year
        derived = current_year - grad_year
        return max(derived, 0)
    except (TypeError, ValueError):
        return None


def project(payload: dict) -> tuple[dict, list[str]]:
    """Project ``payload`` down to the allowlist.

    Returns ``(projected, warnings)`` where:
      - ``projected`` contains ONLY allowlisted fields (proxy variables converted).
      - ``warnings`` records dropped-field categories/reasons — NEVER the content.

    Any field whose *value* contains a sensitive/prohibited lexeme is dropped even
    if its key is allowlisted (defense in depth against sensitive content smuggled
    into a free-text job field).
    """
    if not isinstance(payload, dict):
        return {}, ["dropped:non_dict_payload"]

    projected: dict[str, Any] = {}
    warnings: list[str] = []

    # 1. proxy conversion: derive experience_years, drop raw date proxies.
    exp_years = _to_experience_years(payload)
    if exp_years is not None:
        projected["experience_years"] = exp_years
    for pf in _PROXY_DATE_FIELDS:
        if pf in payload:
            warnings.append(f"dropped_proxy:{pf}")

    # 2. allowlist projection + sensitive-content scan.
    for key, value in payload.items():
        if key in _PROXY_DATE_FIELDS:
            continue  # already handled
        if key == "experience_years":
            continue  # already handled by _to_experience_years
        if key not in ALLOWED_FIELDS:
            warnings.append(f"dropped_field:{key}")
            continue
        # Scan the value for sensitive/prohibited content.
        text = value if isinstance(value, str) else " ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
        cats = detect_sensitive_categories(text)
        if cats:
            # Drop the whole field; record only the category codes, not content.
            warnings.append(f"dropped_sensitive:{key}:{'|'.join(cats)}")
            continue
        projected[key] = value

    return projected, warnings

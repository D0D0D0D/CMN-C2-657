"""candidate_cases DB connection seam (docs/02_design.md §4-2 / §10).

candidate_cases stores ONLY minimized/anonymized code + numeric fields
(job_category / experience_years / skill_tags / fit_scores(axis×sufficiency
codes) / concern_flags(type codes) / hire_outcome / performance_score /
evaluator_edited) — NEVER raw PII, free-text report bodies, or direct
identifiers (§4-2). A case_id-scoped deletion path supports APPI right-to-erasure.

Real client (shared.services.database postgres/mysql via ctx.secrets DSN) is wired
at server.wire_services(); tests inject FakeDB.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

_DB: "CaseStore | None" = None

# Fields allowed in a candidate_cases row — enforced so raw PII cannot be persisted.
ALLOWED_CASE_FIELDS: frozenset[str] = frozenset(
    {
        "case_id", "job_category", "experience_years", "skill_tags",
        "fit_scores", "concern_flags", "hire_outcome", "performance_score",
        "evaluator_edited", "created_at", "updated_at",
    }
)


@runtime_checkable
class CaseStore(Protocol):
    def insert_case(self, record: dict) -> str: ...
    def update_outcome(self, case_id: str, fields: dict) -> bool: ...
    def delete_case(self, case_id: str) -> bool: ...
    def get_case(self, case_id: str) -> dict | None: ...


def set_db(db: "CaseStore | None") -> None:
    global _DB
    _DB = db


def get_db() -> "CaseStore | None":
    return _DB


def sanitize_case_record(record: dict) -> dict:
    """Return a copy containing only ALLOWED_CASE_FIELDS.

    Structural guarantee that no raw PII / free-text is written to candidate_cases,
    regardless of what a caller passes (§4-2 minimization).
    """
    return {k: v for k, v in record.items() if k in ALLOWED_CASE_FIELDS}


def new_case_id() -> str:
    return f"cc-{uuid.uuid4().hex[:12]}"


class FakeDB:
    """In-memory candidate_cases store for tests + local operation.

    Idempotent update_outcome (safe to call twice with the same case_id+outcome).
    """

    def __init__(self, fail_insert: bool = False) -> None:
        self._rows: dict[str, dict] = {}
        self._fail_insert = fail_insert

    def insert_case(self, record: dict) -> str:
        if self._fail_insert:
            raise RuntimeError("simulated candidate_cases INSERT failure")
        rec = sanitize_case_record(record)
        cid = rec.get("case_id") or new_case_id()
        rec["case_id"] = cid
        self._rows[cid] = rec
        return cid

    def update_outcome(self, case_id: str, fields: dict) -> bool:
        if case_id not in self._rows:
            raise KeyError(f"case_id not found: {case_id}")
        # Partial UPDATE of only the outcome columns (idempotent).
        allowed = {k: v for k, v in fields.items()
                   if k in {"hire_outcome", "performance_score", "evaluator_edited"}}
        self._rows[case_id].update(allowed)
        return True

    def delete_case(self, case_id: str) -> bool:
        return self._rows.pop(case_id, None) is not None

    def get_case(self, case_id: str) -> dict | None:
        return self._rows.get(case_id)

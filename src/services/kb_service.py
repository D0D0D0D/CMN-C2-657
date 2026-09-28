"""Recruiting knowledge KB connection seam (docs/02_design.md §7 / §10).

The recruiting KB (own-company hiring criteria + accumulated high-performer traits)
is a vector store. This module is the injection seam: a KBClient protocol plus a
process-wide singleton wired at server startup. Nodes call get_kb(); when the KB is
absent (None) the pipeline degrades explicitly (meta.kb_coverage="degraded") rather
than failing (§10).

Real vector-store wiring (pgvector / pinecone from shared.services.vector_store) is
provided at server.wire_services(); tests inject FakeKB.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

_KB: "KBClient | None" = None


@runtime_checkable
class KBClient(Protocol):
    """Minimal KB contract the assessment nodes rely on."""

    def search_criteria(self, job_category: str, query: str, top_k: int = 5) -> list[dict]:
        """Return own-company hiring-criteria snippets for the query."""

    def cohort_size(self, job_category: str) -> int:
        """Return the number of accumulated high-performer cases for the category."""

    def upsert(self, job_category: str, records: list[dict]) -> int:
        """Upsert normalized (code/numeric only) records; return count written."""


def set_kb(kb: "KBClient | None") -> None:
    global _KB
    _KB = kb


def get_kb() -> "KBClient | None":
    return _KB


class FakeKB:
    """In-memory KB for tests + degraded local operation.

    Deterministic; no network. Seed cohort sizes / criteria to exercise both the
    full and degraded paths.
    """

    def __init__(self, criteria: dict | None = None, cohorts: dict | None = None) -> None:
        self._criteria = criteria or {}
        self._cohorts = dict(cohorts or {})
        self._store: dict[str, list[dict]] = {}

    def search_criteria(self, job_category: str, query: str, top_k: int = 5) -> list[dict]:
        return list(self._criteria.get(job_category, []))[:top_k]

    def cohort_size(self, job_category: str) -> int:
        return int(self._cohorts.get(job_category, 0))

    def upsert(self, job_category: str, records: list[dict]) -> int:
        self._store.setdefault(job_category, []).extend(records)
        self._cohorts[job_category] = self._cohorts.get(job_category, 0) + len(records)
        return len(records)

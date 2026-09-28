"""AgentCore Platform v1.0 — inner workflow nodes G1 (docs/02_design.md §2-2).

InputParseNode (inner entry), IngestNormalizeNode, KbUpsertNode.

All inherit FunctionNode → S-1..S-4 run via BaseNode.__call__. Each execute()
returns ONLY changed keys and calls emit_trace_event() at least once.
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.allowlist import project
from src.services.db_service import get_db, new_case_id, sanitize_case_record
from src.services.kb_service import get_kb


class InputParseNode(FunctionNode):
    """Inner ENTRY node: restore InnerState from the outer JSON envelope (§2-2).

    The inner graph only receives user_input(str)+ctx; without this restoration
    mode_route cannot read `mode`. Parse failure → status=error.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        raw = state.get("user_input", "") or ""
        try:
            envelope = json.loads(raw) if raw.strip().startswith("{") else {"payload": {"job_experience_text": raw}}
        except json.JSONDecodeError:
            emit_trace_event("input_parse_failed", {}, state)
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["InputParseNode: could not parse inner input envelope"]}

        mode = envelope.get("mode", "evaluate")
        payload = envelope.get("payload", {}) or {}
        emit_trace_event("input_parsed", {"mode": mode, "fields": len(payload)}, state)
        return {
            "mode": mode,
            "job_category": envelope.get("job_category", ""),
            "phase": envelope.get("phase", ""),
            "case_id": envelope.get("case_id", ""),
            "validated_payload": payload,
            "degraded_flags": [],
            "status": AgentStatus.SUCCESS.value,
        }


class IngestNormalizeNode(FunctionNode):
    """Normalize CSV/Excel-derived rows via LLM column mapping; apply allowlist.

    Prohibited-attribute columns and direct identifiers are dropped here too
    (same allowlist projection as intake — §2-2 / §4-3-3).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        payload = state.get("validated_payload", {}) or {}
        rows = payload.get("rows") or payload.get("records") or []
        if isinstance(payload, dict) and not rows and "job_experience_text" in payload:
            rows = [payload]

        normalized: list[dict] = []
        dropped_cols: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            proj, warnings = project(row)
            for w in warnings:
                # record only the dropped column name/category (never content)
                dropped_cols.add(w)
            # candidate_cases rows are code/numeric only.
            normalized.append(sanitize_case_record({
                "job_category": state.get("job_category", "") or proj.get("job_category", ""),
                "experience_years": proj.get("experience_years"),
                "skill_tags": proj.get("skill_tags", []),
                "hire_outcome": row.get("hire_outcome", "pending"),
                "performance_score": row.get("performance_score"),
                "evaluator_edited": False,
            }))

        emit_trace_event("ingest_normalized", {"rows": len(normalized), "dropped": len(dropped_cols)}, state)
        return {
            "ingest_summary": {"normalized_count": len(normalized),
                               "dropped_columns": sorted(dropped_cols)},
            "validated_payload": {**payload, "_normalized": normalized},
            "status": AgentStatus.SUCCESS.value,
        }


class KbUpsertNode(FunctionNode):
    """Upsert normalized (code/numeric) rows into KB / candidate_cases (§2-2)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        payload = state.get("validated_payload", {}) or {}
        normalized = payload.get("_normalized", [])
        job_category = state.get("job_category", "")
        summary = dict(state.get("ingest_summary", {}))
        degraded = list(state.get("degraded_flags", []))

        kb = get_kb()
        db = get_db()
        kb_written = 0
        db_written = 0
        if kb is not None and normalized:
            try:
                kb_written = kb.upsert(job_category, normalized)
            except Exception:
                degraded.append("kb_upsert_failed")
        else:
            degraded.append("kb_unavailable")
        if db is not None and normalized:
            for rec in normalized:
                try:
                    rec = dict(rec)
                    rec["case_id"] = new_case_id()
                    db.insert_case(rec)
                    db_written += 1
                except Exception:
                    degraded.append("case_insert_failed")

        summary.update({"kb_written": kb_written, "db_written": db_written})
        emit_trace_event("ingest_upserted", {"kb": kb_written, "db": db_written}, state)
        return {
            "ingest_summary": summary,
            "assessment_payload": {"mode": "ingest", "ingest_summary": summary},
            "degraded_flags": degraded,
            "status": AgentStatus.SUCCESS.value,
        }

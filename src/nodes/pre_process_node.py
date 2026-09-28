"""AgentCore Platform v1.0 — CandidateIntakeNode (outer pre_process).

docs/02_design.md §2-1 / §4-2 / §5-2. Validates mode, projects the candidate
payload down to the allowlist (structural exclusion of prohibited attributes and
proxy variables), records dropped-field categories in intake_warnings (content
never recorded), enforces an input size ceiling, and flags large documents for
staged summarization.

S-2 domain checks live in _extra_security_gate_input() only — the @final
_security_gate_input() (default PII scan) is inherited from FunctionNode and MUST
NOT be overridden.
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

VALID_MODES = ("ingest", "evaluate", "feedback")
_MAX_INPUT_CHARS = 200_000  # hard ceiling (§4-2 (c))

# Control fields that may pass through for ingest/feedback modes. Row-level candidate
# data inside `rows` is allowlist-projected PER-ROW downstream (IngestNormalizeNode).
_CONTROL_FIELDS = ("mode", "job_category", "phase", "case_id", "rows", "records",
                   "hire_outcome", "performance_score", "evaluator_edited",
                   "experience_years", "skill_tags", "job_experience_text")


def _passthrough_control_payload(payload: dict) -> tuple[dict, list[str]]:
    """Pass through structured control fields for ingest/feedback (no candidate free-text projection).

    Row-level candidate data is projected per-row downstream; here we only forward
    the control/structured fields and drop anything unexpected (recorded, not content).
    """
    if not isinstance(payload, dict):
        return {}, ["dropped:non_dict_payload"]
    out: dict = {}
    warnings: list[str] = []
    for k, v in payload.items():
        if k in _CONTROL_FIELDS:
            out[k] = v
        else:
            warnings.append(f"dropped_field:{k}")
    return out, warnings


class CandidateIntakeNode(FunctionNode):
    """Validate + allowlist-project incoming candidate input (S-2 input boundary)."""

    # S-1 (§5-1): candidate personal-data processing + DB/RAG writes = privileged.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """S-2 domain hook: size ceiling only (must not raise; surface via state).

        Allowlist projection + sensitive-attribute drop happens in execute() so the
        projected result and warnings can flow into downstream State; the gate here
        performs the fail-fast size check per §4-2(c).
        """
        raw = state.get("user_input", "") or ""
        if isinstance(raw, str) and len(raw) > _MAX_INPUT_CHARS:
            state = dict(state)  # type: ignore[assignment]
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", [])
            state["error_log"] = list(state.get("error_log", [])) + [
                f"CandidateIntakeNode: input exceeds size ceiling ({len(raw)} > {_MAX_INPUT_CHARS})"
            ]
        return state  # type: ignore[return-value]

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "") or ""
        input_context = state.get("input_context", {}) or {}  # read-only

        # mode is supplied by the API adapter via input_context (§3).
        mode = (input_context.get("mode") or state.get("mode") or "evaluate")
        if mode not in VALID_MODES:
            emit_trace_event("intake_rejected", {"reason": "unknown_mode", "mode": str(mode)[:32]}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CandidateIntakeNode: unknown mode '{mode}' (expected {VALID_MODES})"],
            }

        # Parse the structured candidate payload (JSON) if present; else treat as free text.
        payload: dict = {}
        if user_input.strip().startswith("{"):
            try:
                payload = json.loads(user_input)
            except json.JSONDecodeError:
                payload = {"job_experience_text": user_input}
        else:
            payload = {"job_experience_text": user_input}

        # Allowlist projection = primary structural defense for the EVALUATE candidate
        # payload (§4-2 / §4-3). For ingest, the same allowlist projection is applied
        # PER-ROW inside IngestNormalizeNode (§4-3-3); for feedback the payload is only
        # control fields (case_id + outcome columns). So evaluate projects here; ingest
        # / feedback carry the structured control payload through to the inner nodes.
        if mode == "evaluate":
            projected, warnings = project(payload)
        else:
            projected, warnings = _passthrough_control_payload(payload)

        # Merge adapter-boundary prefilter warnings (server.prepare_input dropped
        # sensitive fields BEFORE the graph so they never reach any checkpoint —
        # §5-0; the drop facts are surfaced here in intake_warnings).
        prefilter_warnings = input_context.get("prefilter_warnings") or []
        if isinstance(prefilter_warnings, list):
            warnings = list(prefilter_warnings) + warnings

        # feedback mode requires a case_id (from payload or input_context).
        case_id = payload.get("case_id") or input_context.get("case_id", "")
        if mode == "feedback" and not case_id:
            # S-4: audit the rejection (docs/02 §5-4 — every execute() path emits).
            emit_trace_event("intake_feedback_missing_case_id", {"mode": mode}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CandidateIntakeNode: feedback mode requires case_id"],
            }

        job_category = str(projected.get("job_category") or input_context.get("job_category") or "").strip()
        if mode == "evaluate" and not job_category and not projected.get("job_experience_text"):
            # Nothing usable survived the allowlist projection (§10 "input insufficient").
            emit_trace_event("intake_insufficient", {"warnings": len(warnings)}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "intake_warnings": warnings,
                "error_log": ["CandidateIntakeNode: no job-related information usable after allowlist projection"],
            }

        phase = str(projected.get("phase") or input_context.get("phase") or "").strip()
        summarize_needed = len(user_input) > int(input_context.get("summarize_threshold_chars", 20_000))
        if summarize_needed:
            warnings.append("staged_summarization_flagged")

        # validated_input carries ONLY allowlist-projected, tokenized-upstream data.
        validated_input = json.dumps(
            {"mode": mode, "job_category": job_category, "phase": phase,
             "case_id": case_id, "payload": projected},
            ensure_ascii=False, default=str,
        )

        emit_trace_event(
            "intake_completed",
            {"mode": mode, "dropped": len(warnings), "job_category_present": bool(job_category)},
            state,
        )
        return {
            "mode": mode,
            "validated_input": validated_input,
            "job_category": job_category,
            "phase": phase,
            "case_id": case_id,
            "intake_warnings": warnings,
            "status": AgentStatus.SUCCESS.value,
        }

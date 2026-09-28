"""AgentCore Platform v1.0 — inner HITL + write nodes (docs/02_design.md §6 / §2-2).

HitlReviewNode (D6 interrupt), CaseInsertNode, FeedbackUpdateNode.
"""

from __future__ import annotations

from typing import ClassVar

from langgraph.types import interrupt

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus, extract_action
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.db_service import get_db, new_case_id, sanitize_case_record


class HitlReviewNode(FunctionNode):
    """D6 interrupt: present the report draft; receive approve/edit/request-revision.

    §6 branches:
      - approve         → case_insert (evaluator_edited=false)
      - edit            → case_insert (evaluator_edited=true), edits merged
      - request-revision→ route back to question_generation via conditional edge
    hitl_draft snapshots the draft so resume does not re-run the LLM (idempotency).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        # Assemble the draft presented to the human reviewer.
        draft = {
            "fit_findings": state.get("fit_findings", []),
            "axis_assessments": state.get("axis_assessments", []),
            "concern_flags": state.get("concern_flags", []),
            "interview_questions": state.get("interview_questions", []),
            "kb_coverage": (state.get("assessment_payload") or {}).get("kb_coverage", "full"),
        }

        # MANDATORY hitl_allowed guard: when this
        # graph runs inside an automated pipeline (parent GraphNode sets
        # hitl_allowed=False), an unconditional interrupt() would deadlock the
        # composition. Skip human review and return the AI draft as-is —
        # authorship stays "ai_draft" and human_review records the skip.
        if not state.get("hitl_allowed", True):
            emit_trace_event("hitl_skipped_not_allowed", {"reason": "hitl_allowed=false"}, state)
            return {
                "hitl_draft": draft,
                "report_draft": {
                    **draft,
                    "human_review": {"status": "skipped_hitl_not_allowed", "edited_sections": []},
                    "_evaluator_edited": False,
                },
                "status": AgentStatus.SUCCESS.value,
            }

        # D6 interrupt — suspends here; on resume `feedback` is the human response.
        feedback = interrupt({"draft": draft, "reason": "human review required"})

        action = (extract_action(feedback) or "approve").lower()
        emit_trace_event("hitl_resolved", {"action": action}, state)

        if action in ("request-revision", "correct", "revision"):
            directive = feedback.get("directive") if isinstance(feedback, dict) else str(feedback)
            # Route back to regeneration (conditional edge reads status=retry).
            return {
                "hitl_draft": draft,
                "hitl_feedback": feedback,
                "hitl_status": HitlStatus.CORRECTED.value,
                "revision_directive": directive,
                "status": AgentStatus.RETRY.value,
            }

        edited = action in ("edit", "edited")
        human_review = {"status": "edited" if edited else "confirmed",
                        "edited_sections": (feedback.get("edited_sections", []) if isinstance(feedback, dict) else [])}
        # Merge any human edits (only for the sections they touched).
        if edited and isinstance(feedback, dict) and feedback.get("edits"):
            draft.update(feedback["edits"])

        return {
            "hitl_draft": draft,
            "hitl_feedback": feedback,
            "hitl_status": HitlStatus.APPROVED.value,
            "report_draft": {**draft, "human_review": human_review, "_evaluator_edited": edited},
            "status": AgentStatus.SUCCESS.value,
        }


class CaseInsertNode(FunctionNode):
    """INSERT minimized/anonymized case (hire_outcome=pending); mint case_id.

    DB write failure does NOT block the evaluation return — it degrades (§2-2 / §10).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        draft = state.get("report_draft", {}) or {}
        degraded = list(state.get("degraded_flags", []))
        job_category = state.get("job_category", "")
        evaluator_edited = bool(draft.get("_evaluator_edited"))

        # candidate_cases row = code/numeric only (§4-2). sanitize enforces it.
        record = sanitize_case_record({
            "case_id": state.get("case_id") or new_case_id(),
            "job_category": job_category,
            "experience_years": (state.get("validated_payload") or {}).get("experience_years"),
            "skill_tags": (state.get("validated_payload") or {}).get("skill_tags", []),
            "fit_scores": [{"axis": a.get("axis"), "sufficiency": a.get("sufficiency")}
                           for a in state.get("axis_assessments", [])],
            "concern_flags": [c.get("code") for c in state.get("concern_flags", [])],
            "hire_outcome": "pending",
            "evaluator_edited": evaluator_edited,
        })

        db = get_db()
        case_id = record["case_id"]
        if db is not None:
            try:
                case_id = db.insert_case(record)
            except Exception:
                degraded.append("case_insert_failed")
                case_id = ""  # no case_id returned on failure (§10 re-run guidance)
        else:
            degraded.append("db_unavailable")

        assessment_payload = {
            **(state.get("assessment_payload") or {}),
            "fit_findings": state.get("fit_findings", []),
            "axis_assessments": state.get("axis_assessments", []),
            "concern_flags": state.get("concern_flags", []),
            "interview_questions": state.get("interview_questions", []),
            "similar_cases": [{"summary": f"匿名集計のみ（個票は返さない） job_category={job_category}"}],
            "human_review": draft.get("human_review", {"status": "confirmed", "edited_sections": []}),
        }
        emit_trace_event("case_inserted", {"case_id": bool(case_id), "degraded": len(degraded)}, state)
        return {
            "case_id": case_id,
            "assessment_payload": assessment_payload,
            "degraded_flags": degraded,
            "status": AgentStatus.SUCCESS.value,
        }


class FeedbackUpdateNode(FunctionNode):
    """Partial, idempotent UPDATE of outcome columns only (§2-2). Missing case_id errors."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        case_id = state.get("case_id", "")
        payload = state.get("validated_payload", {}) or {}
        if not case_id:
            # S-4: audit every failure path (docs/02 §5-4).
            emit_trace_event("feedback_missing_case_id", {}, state)
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["FeedbackUpdateNode: case_id is required"]}

        fields = {k: payload[k] for k in ("hire_outcome", "performance_score", "evaluator_edited")
                  if k in payload}
        db = get_db()
        if db is None:
            emit_trace_event("feedback_db_unwired", {}, state)
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["FeedbackUpdateNode: DB not wired"]}
        try:
            db.update_outcome(case_id, fields)   # idempotent partial UPDATE
        except KeyError:
            emit_trace_event("feedback_case_not_found", {"fields": sorted(fields)}, state)
            return {"status": AgentStatus.ERROR.value,
                    "error_log": [f"FeedbackUpdateNode: case_id not found ({case_id}) — confirm case_id"]}

        emit_trace_event("feedback_updated", {"case_id": True, "fields": sorted(fields)}, state)
        return {
            "assessment_payload": {"mode": "feedback", "case_id": case_id, "updated_fields": sorted(fields)},
            "status": AgentStatus.SUCCESS.value,
        }

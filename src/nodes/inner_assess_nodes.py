"""AgentCore Platform v1.0 — inner workflow nodes G2 (docs/02_design.md §2-2 / §2-5).

FitAnalysisNode, PotentialAssessmentNode, VerificationPointsNode,
QuestionGenerationNode.

Non-negotiables implemented literally:
  - potential_assessment emits ONLY per-axis sufficiency (high/medium/low/unknown)
    + evidence — NEVER an overall/composite score or ranking (§2-5);
  - population below population_threshold → explicit degradation (states the sample
    is too small; still no composite score) (§2-2 / §10);
  - every generated question carries a targets_concern reference id (§2-2).

LLM access via the injected shared.services.llm.BaseLLM (get_llm()); when absent,
degrade deterministically (kb_coverage="degraded") rather than fail.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.kb_service import get_kb
from src.services.llm_client import get_llm

_DEFAULT_POPULATION_THRESHOLD = 20


class FitAnalysisNode(FunctionNode):
    """Candidate (allowlist-projected) × own-company criteria (KB) with evidence."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        payload = state.get("validated_payload", {}) or {}
        job_category = state.get("job_category", "")
        degraded = list(state.get("degraded_flags", []))

        kb = get_kb()
        criteria = []
        kb_coverage = "full"
        if kb is not None:
            try:
                criteria = kb.search_criteria(job_category, payload.get("job_experience_text", ""), top_k=5)
            except Exception:
                degraded.append("kb_search_failed")
                kb_coverage = "degraded"
        if not criteria:
            kb_coverage = "degraded"
            if "kb_unavailable" not in degraded:
                degraded.append("kb_criteria_empty")

        text = payload.get("job_experience_text", "")
        interview = payload.get("interview_response_text", "")
        llm = get_llm()
        findings: list[dict] = []
        # Build evidence-anchored findings. Every finding MUST carry evidence; if no
        # supporting quote can be constructed, alignment falls to "unknown" (§2-5).
        for axis in _axes_from_criteria(criteria):
            quote = _first_evidence(text, interview)
            findings.append({
                "axis": axis,
                "alignment": "match" if quote else "unknown",
                "evidence": ([{"source": "interview_memo" if interview else "resume", "quote": quote}]
                             if quote else []),
                "note": (llm.complete([{"role": "user", "content": f"fit note for {axis}"}])["content"]
                         if llm else "criteria comparison (degraded: no LLM wired)"),
            })
        if not findings:
            # No criteria & no evidence — one unknown placeholder so the report is honest.
            findings.append({"axis": "general_fit", "alignment": "unknown", "evidence": [],
                             "note": "自社基準KB未整備のため一般観点で評価 (degraded)"})

        emit_trace_event("fit_analyzed", {"findings": len(findings), "kb": kb_coverage}, state)
        return {
            "fit_findings": findings,
            "assessment_payload": {**(state.get("assessment_payload") or {}), "kb_coverage": kb_coverage},
            "degraded_flags": degraded,
            "status": AgentStatus.SUCCESS.value,
        }


class PotentialAssessmentNode(FunctionNode):
    """Per-axis sufficiency vs accumulated high-performer traits — NO composite score.

    Below population_threshold → explicit degradation (sample-too-small stated in
    evidence); a single composite/overall number is NEVER emitted (§2-5).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, population_threshold: int = _DEFAULT_POPULATION_THRESHOLD) -> None:
        self._threshold = int(population_threshold)

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        job_category = state.get("job_category", "")
        fit_findings = state.get("fit_findings", []) or []
        degraded = list(state.get("degraded_flags", []))

        kb = get_kb()
        cohort = 0
        if kb is not None:
            try:
                cohort = kb.cohort_size(job_category)
            except Exception:
                degraded.append("cohort_lookup_failed")

        sample_too_small = cohort < self._threshold
        if sample_too_small and "cohort_below_threshold" not in degraded:
            degraded.append("cohort_below_threshold")

        axis_assessments: list[dict] = []
        for f in fit_findings:
            axis = f.get("axis", "unknown")
            has_evidence = bool(f.get("evidence"))
            # sufficiency is a categorical judgment — NEVER a numeric score.
            if not has_evidence:
                sufficiency = "unknown"
            elif sample_too_small:
                sufficiency = "unknown"  # cannot substantiate without cohort backing
            else:
                sufficiency = "medium"
            evidence = list(f.get("evidence", []))
            if sample_too_small:
                evidence.append({"source": "candidate_cases",
                                 "stat": f"母集団不足(n={cohort} < 閾値{self._threshold}) — 統計裏付け弱いため縮退"})
            else:
                evidence.append({"source": "candidate_cases", "stat": f"職種内 n={cohort} の匿名集計"})
            axis_assessments.append({
                "axis": axis,
                "sufficiency": sufficiency,   # high/medium/low/unknown ONLY
                "concerns": [],
                "evidence": evidence,
            })

        emit_trace_event(
            "potential_assessed",
            {"axes": len(axis_assessments), "cohort": cohort, "degraded_sample": sample_too_small},
            state,
        )
        return {
            "axis_assessments": axis_assessments,
            "degraded_flags": degraded,
            "status": AgentStatus.SUCCESS.value,
        }


class VerificationPointsNode(FunctionNode):
    """Extract unverified concerns / skills to confirm → concern_flags (§2-2)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        axis_assessments = state.get("axis_assessments", []) or []
        concern_flags: list[dict] = []
        for i, a in enumerate(axis_assessments, start=1):
            if a.get("sufficiency") in ("unknown", "low"):
                cid = f"c-{i:02d}"
                concern_flags.append({
                    "concern_id": cid,
                    "code": f"axis_{a.get('axis','unknown')}",
                    "description": f"{a.get('axis','?')} の活躍可能性が未確認/裏付け不足",
                    "why_unverified": "evidence が不足、または母集団縮退のため",
                })
                a["concerns"] = list(a.get("concerns", [])) + [cid]
        emit_trace_event("verification_extracted", {"concerns": len(concern_flags)}, state)
        return {
            "concern_flags": concern_flags,
            "axis_assessments": axis_assessments,
            "status": AgentStatus.SUCCESS.value,
        }


class QuestionGenerationNode(FunctionNode):
    """Generate structured interview questions + rubrics; each targets a concern.

    A 'request-revision' HITL directive routes back here (§2-2 / §6). On a revision
    pass, `revision_directive` biases the generated question text.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def execute(self, state: dict) -> dict:
        concern_flags = state.get("concern_flags", []) or []
        directive = state.get("revision_directive")
        llm = get_llm()
        questions: list[dict] = []
        for i, c in enumerate(concern_flags, start=1):
            base = f"{c.get('description','')} を確認する設問"
            if directive:
                base = f"[修正指示反映] {directive}: {base}"
            q_text = (llm.complete([{"role": "user", "content": base}])["content"]
                      if llm else base)
            questions.append({
                "question_id": f"q-{i:02d}",
                "targets_concern": c.get("concern_id", f"c-{i:02d}"),  # MANDATORY reference id (§2-2)
                "question": q_text,
                "rubric": {"good": "具体的な行動事実で回答",
                           "acceptable": "部分的に具体",
                           "insufficient": "一般論のみ"},
            })
        emit_trace_event("questions_generated", {"questions": len(questions), "revision": bool(directive)}, state)
        return {
            "interview_questions": questions,
            "revision_directive": None,   # clear after applying
            "status": AgentStatus.SUCCESS.value,
        }


# --- helpers (pure) ---

def _axes_from_criteria(criteria: list) -> list[str]:
    axes = [c.get("axis") for c in criteria if isinstance(c, dict) and c.get("axis")]
    return axes or (["role_skill", "collaboration"] if criteria else [])


def _first_evidence(text: str, interview: str) -> str:
    src = interview or text
    if not src:
        return ""
    return src.strip().split("\n")[0][:200]

"""AgentCore Platform v1.0 — ReportComposeNode (outer post_process).

docs/02_design.md §2-1 / §2-5 / §5-3. Consolidates the assessment_payload from the
inner subgraph into the final report (structured JSON + Markdown) and applies the
S-3 output gate via _extra_security_gate_output():
  1. assertive-phrase DOWNGRADE (decisive verdict → "requires confirmation"),
  2. prohibited-attribute mention scan/redaction,
  3. structural non-assertive contract enforcement (no pass/fail/score keys),
  4. PII re-scan on all output strings (belt-and-braces; FunctionNode's @final S-3
     credential scan already runs first).

The @final _security_gate_output() (default credential scan) is inherited and MUST
NOT be overridden; domain checks live in _extra_security_gate_output() only.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.services.assertive_guard import apply_output_guards
from src.services.report_builder import build_report


class ReportComposeNode(FunctionNode):
    """Assemble the final assessment report and apply S-3 domain output guards."""

    # S-1 (§5-1): privileged (handles candidate assessment output).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        # Upstream error pass-through (§2-1): do not compose over an errored state.
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("report_skipped_error", {}, state)
            return {"status": AgentStatus.ERROR.value}

        payload = state.get("assessment_payload") or {}
        mode = state.get("mode", "evaluate")

        # ingest / feedback modes have no report to compose — pass their summary through.
        if mode in ("ingest", "feedback"):
            emit_trace_event("report_composed", {"mode": mode, "report": False}, state)
            return {"result": payload, "status": AgentStatus.SUCCESS.value}

        report = build_report(
            case_id=state.get("case_id", ""),
            job_category=state.get("job_category", ""),
            phase=state.get("phase", ""),
            kb_coverage=payload.get("kb_coverage", "full"),
            fit_findings=payload.get("fit_findings", []),
            axis_assessments=payload.get("axis_assessments", []),
            concern_flags=payload.get("concern_flags", []),
            interview_questions=payload.get("interview_questions", []),
            similar_cases=payload.get("similar_cases", []),
            human_review=payload.get("human_review"),
        )

        emit_trace_event(
            "report_composed",
            {"mode": mode, "axes": len(report.get("potential_assessment", [])),
             "questions": len(report.get("interview_questions", []))},
            state,
        )
        return {"result": report, "status": AgentStatus.SUCCESS.value}

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain output guard (§5-3). Runs after FunctionNode credential scan."""
        report = result.get("result")
        if not isinstance(report, dict):
            return result

        # 1-3. assertive downgrade + prohibited-attribute redaction + structural contract.
        # apply_output_guards RAISES on a forbidden pass/fail/score key (§2-5) — never repaired.
        guarded, degraded = apply_output_guards(report)

        # 4. PII re-scan on all string values (mask any identifier that slipped into a quote).
        _rescan_pii(guarded)

        result["result"] = guarded
        if guarded.get("markdown") is not None:
            # keep markdown consistent with the (possibly) guarded structured fields
            from src.services.report_builder import _render_markdown
            guarded["markdown"] = _render_markdown(guarded)
        return result


def _rescan_pii(obj: object) -> None:
    """In-place PII masking on every string leaf of a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                obj[k] = _mask(v)
            else:
                _rescan_pii(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = _mask(v)
            else:
                _rescan_pii(v)


def _mask(text: str) -> str:
    findings = detect_pii(text)
    if not findings:
        return text
    out = text
    for f in sorted(findings, key=lambda x: x["start"], reverse=True):
        out = out[: f["start"]] + "[MASKED]" + out[f["end"]:]
    return out

"""AgentCore Platform v1.0 — CandidateAssessmentGraphNode (outer main).

docs/02_design.md §2-1 / §2-2 / §6. GraphNode that wraps the inner
CandidateAssessmentWorkflow. Implements the verified GraphNode contract:

  - get_subgraph()  → returns a CACHED self._subgraph built once in __init__
                      (rebuilding per call loses the checkpointer on resume and
                      spins an interrupt-resume infinite loop — §2-2).
  - extract_input() → JSON envelope (default=str) so the inner InputParseNode can
                      restore InnerState (outer input_context is NOT propagated).
  - merge_output()  → ONLY changed keys, including hitl_draft/hitl_feedback/
                      hitl_status so HITL surfaces to the outer caller.
  - error_strategy = "handle" (accumulation-write failure degrades; evaluation
                      failure returns status=error).
  - propagate_hitl = True (surface inner interrupt to server.py).
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from shared.utils.audit_logger import emit_trace_event


class CandidateAssessmentGraphNode(GraphNode):
    """Outer `main` slot — drives the inner CandidateAssessmentWorkflow subgraph."""

    # Accumulation-write failures degrade (evaluation still returned); evaluation
    # failures return status=error via the inner graph (§2-2 error_strategy).
    error_strategy: ClassVar[str] = "handle"
    # Surface the inner HITL interrupt to the outer caller (server.py). Requires
    # hitl.enabled: true in the outer config too (§6).
    propagate_hitl: ClassVar[bool] = True

    # S-1: privileged (candidate data + DB/RAG writes flow through the subgraph).
    from framework.schemas.trust_level import TrustLevel as _TL
    required_trust_level: ClassVar[_TL] = _TL.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        # Cache the inner graph ONCE — its checkpointer must survive across
        # invoke()/resume() calls (§2-2). Rebuilding per get_subgraph() call
        # would drop the suspended checkpoint and loop forever on resume.
        from src.graph.domain_workflow_graph import CandidateAssessmentWorkflow
        self._subgraph = CandidateAssessmentWorkflow(config=config or {})

    def get_subgraph(self):
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        """Pass validated_input (already a JSON envelope) straight through.

        CandidateIntakeNode produced validated_input as json.dumps({mode, ...,
        payload}); the inner InputParseNode json.loads it back into InnerState.
        """
        return state.get("validated_input", state.get("user_input", "")) or ""

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map inner get_output() back into outer state — ONLY changed keys."""
        emit_trace_event(
            "assessment_merged",
            {"status": sub_result.get("status"), "degraded": len(sub_result.get("degraded_flags", []) or [])},
            state,
        )
        delta: dict = {
            "assessment_payload": sub_result.get("assessment_payload", sub_result.get("output", {})),
            "case_id": sub_result.get("case_id", state.get("case_id", "")),
            "degraded_flags": sub_result.get("degraded_flags", []),
            "status": sub_result.get("status", AgentStatus.SUCCESS.value),
        }
        # Surface HITL fields so the outer caller (server.py) can round-trip resume.
        for k in ("hitl_draft", "hitl_feedback", "hitl_status"):
            if k in sub_result:
                delta[k] = sub_result[k]
        return delta

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict:
        """error_strategy='handle' — distinguish validation errors from write failures.

        Accumulation-write failures (DB/KB) are already handled INSIDE the inner
        nodes as degradation (degraded_flags, no error status) — the evaluation is
        always returned. So reaching here means either:
          (a) the inner graph returned status=error for a genuine VALIDATION failure
              (unknown mode, missing/unknown case_id, unparseable input) — these MUST
              surface as status=error (§2-2 "evaluation-core / validation failures
              return status=error"); or
          (b) an unexpected exception was raised — surface it as error too (fail safe:
              we never fabricate a passing evaluation over an unknown failure).

        A SubgraphError carries the inner error_log; preserve it for the caller.
        """
        error_log = list(getattr(error, "error_log", None) or [str(error)])
        emit_trace_event("assessment_error", {"error": type(error).__name__}, state)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": error_log,
            "degraded_flags": ["assessment_subgraph_error"],
        }

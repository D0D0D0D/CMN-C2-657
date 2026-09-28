"""AgentCore Platform v1.0 — CandidateAssessmentWorkflow (inner graph).

docs/02_design.md §2-2 / §6. Inner BaseGraph with a custom 3-mode topology plus a
HITL revision loop. Instantiated (and cached) by CandidateAssessmentGraphNode in
src/graph/graph.py.

Topology:
    START → input_parse → {mode_route}
        ├ ingest   → ingest_normalize → kb_upsert ─────────────────────────→ END
        ├ evaluate → fit_analysis → potential_assessment → verification_points
        │            → question_generation → hitl_review → {hitl_route}
        │                 ├ retry(request-revision) → question_generation (loop)
        │                 ├ success                 → case_insert → END
        │                 └ error                   → END
        └ feedback → feedback_update ──────────────────────────────────────→ END

CRITICAL: mode_route / hitl_route / get_output are annotated ``state: InnerState``
— never ``AgentState`` — so LangGraph does not project away the custom inner
fields (docs/02 §2-2 known bug class).
"""

from __future__ import annotations

from typing import ClassVar

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.inner_assess_nodes import (
    FitAnalysisNode,
    PotentialAssessmentNode,
    QuestionGenerationNode,
    VerificationPointsNode,
)
from src.nodes.inner_hitl_nodes import CaseInsertNode, FeedbackUpdateNode, HitlReviewNode
from src.nodes.inner_intake_nodes import IngestNormalizeNode, InputParseNode, KbUpsertNode
from src.schemas.state import InnerState

_DEFAULT_POPULATION_THRESHOLD = 20


class CandidateAssessmentWorkflow(BaseGraph):
    """Inner multi-step assessment workflow (BaseGraph, custom topology)."""

    # Enable HITL-aware invoke() checkpoint config (BaseGraph.invoke reads self.config).
    _hitl_enabled: ClassVar[bool] = True

    @property
    def name(self) -> str:
        return "candidate_assessment_workflow"

    @property
    def state_schema(self) -> type:
        return InnerState

    def _validate_config(self) -> None:
        # No mandatory config; population_threshold is optional (defaulted).
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize/finalize (outer backbone concern).
        threshold = int(self.config.get("population_threshold", _DEFAULT_POPULATION_THRESHOLD))
        self._nodes["input_parse"] = InputParseNode()
        self._nodes["ingest_normalize"] = IngestNormalizeNode()
        self._nodes["kb_upsert"] = KbUpsertNode()
        self._nodes["fit_analysis"] = FitAnalysisNode()
        self._nodes["potential_assessment"] = PotentialAssessmentNode(population_threshold=threshold)
        self._nodes["verification_points"] = VerificationPointsNode()
        self._nodes["question_generation"] = QuestionGenerationNode()
        self._nodes["hitl_review"] = HitlReviewNode()
        self._nodes["case_insert"] = CaseInsertNode()
        self._nodes["feedback_update"] = FeedbackUpdateNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "input_parse")
        # mode branch off input_parse.
        self._sg.add_conditional_edges(
            "input_parse", self.mode_route,
            {"ingest": "ingest_normalize", "evaluate": "fit_analysis",
             "feedback": "feedback_update", "error": END},
        )
        # ingest branch
        self._sg.add_edge("ingest_normalize", "kb_upsert")
        self._sg.add_edge("kb_upsert", END)
        # feedback branch
        self._sg.add_edge("feedback_update", END)
        # evaluate branch
        self._sg.add_edge("fit_analysis", "potential_assessment")
        self._sg.add_edge("potential_assessment", "verification_points")
        self._sg.add_edge("verification_points", "question_generation")
        self._sg.add_edge("question_generation", "hitl_review")
        # HITL outcome branch (request-revision loops back to question_generation).
        self._sg.add_conditional_edges(
            "hitl_review", self.hitl_route,
            {"retry": "question_generation", "success": "case_insert", "error": END},
        )
        self._sg.add_edge("case_insert", END)

    # -- routing functions: annotate with InnerState (never AgentState) --

    def mode_route(self, state: InnerState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return "error"
        mode = state.get("mode", "evaluate")
        return mode if mode in ("ingest", "evaluate", "feedback") else "error"

    def hitl_route(self, state: InnerState) -> str:
        status = state.get("status")
        if status == AgentStatus.RETRY.value:
            return "retry"
        if status == AgentStatus.ERROR.value:
            return "error"
        return "success"

    def route(self, state: InnerState) -> str:
        # Required by BaseGraph ABC. The real branching uses mode_route / hitl_route
        # via add_conditional_edges; this generic route is a defensive fallback.
        return END if state.get("status") == AgentStatus.ERROR.value else "input_parse"

    def get_output(self, state: InnerState) -> dict:
        return {
            "assessment_payload": state.get("assessment_payload", {}),
            "case_id": state.get("case_id", ""),
            "degraded_flags": state.get("degraded_flags", []),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
            # surface HITL fields for the outer GraphNode.merge_output()
            "hitl_draft": state.get("hitl_draft"),
            "hitl_feedback": state.get("hitl_feedback"),
            "hitl_status": state.get("hitl_status"),
        }

    def compile(self, checkpointer: BaseCheckpointSaver | None = None) -> None:
        """Compile with an EXPLICIT checkpointer (HITL requires one — §6 / real-machine).

        BaseGraph.compile() does NOT auto-derive a checkpointer from config in this
        SDK version. When none is supplied, default to an InMemorySaver so interrupt/
        resume works out of the box (server.py swaps in a durable saver for STG; tests
        inject their own). config also carries hitl.enabled so BaseGraph.invoke()
        attaches the thread config.
        """
        self.config.setdefault("hitl", {"enabled": True, "max_hitl": 8})
        self.config.setdefault("memory_enabled", True)
        if checkpointer is None:
            from langgraph.checkpoint.memory import InMemorySaver
            checkpointer = InMemorySaver()
        super().compile(checkpointer=checkpointer)

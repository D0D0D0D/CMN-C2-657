"""AgentCore Platform v1.0 — CMN-C2-657 outer graph (docs/02_design.md §2-1).

Cat 2: AgentBaseGraph fixed 5-node backbone; domain complexity is encapsulated in
the `main` slot via CandidateAssessmentGraphNode (GraphNode wrapping the inner
CandidateAssessmentWorkflow). add_edges()/route() are NOT overridden — backbone
wiring belongs to the framework.
"""

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import CandidateAssessmentGraphNode
from src.nodes.post_process_node import ReportComposeNode
from src.nodes.pre_process_node import CandidateIntakeNode
from src.schemas.state import State


class CandidateFitReportGraph(AgentBaseGraph):
    """CMN-C2-657 outer graph — fixed 5-node backbone (docs/02_design.md §1).

    initialize / finalize are injected by super().register_nodes(); the three
    domain slots are pre_process (CandidateIntakeNode), main
    (CandidateAssessmentGraphNode), post_process (ReportComposeNode).
    """

    @property
    def name(self) -> str:
        return "cmn_c2_657_candidate_fit_report"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = CandidateIntakeNode()
        # Pass config down so the inner graph reads population_threshold etc.
        self._nodes["main"] = CandidateAssessmentGraphNode(config=self.config)
        self._nodes["post_process"] = ReportComposeNode()

    # add_edges() / route() are NOT overridden (framework backbone).


# Backward-compatible alias (scaffold name) — prefer CandidateFitReportGraph.
Graph = CandidateFitReportGraph

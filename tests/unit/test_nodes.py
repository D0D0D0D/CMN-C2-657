"""Node unit tests + framework compliance (TC-01..TC-08, docs/03_test_spec.md)."""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.inner_assess_nodes import PotentialAssessmentNode
from src.nodes.inner_hitl_nodes import CaseInsertNode, FeedbackUpdateNode
from src.nodes.inner_intake_nodes import InputParseNode
from src.nodes.main_node import CandidateAssessmentGraphNode
from src.nodes.post_process_node import ReportComposeNode
from src.nodes.pre_process_node import CandidateIntakeNode


# ── TC-08 / S-1: every node declares required_trust_level >= VERIFIED_EXTERNAL ──
ALL_NODE_CLASSES = [
    CandidateIntakeNode, ReportComposeNode, CandidateAssessmentGraphNode,
    InputParseNode, PotentialAssessmentNode, CaseInsertNode, FeedbackUpdateNode,
]


@pytest.mark.parametrize("cls", ALL_NODE_CLASSES)
def test_tc08_trust_level_declared(cls):
    """TC-08 / S-1 TrustLevel: declared explicitly and is a valid enum value."""
    tl = cls.__dict__.get("required_trust_level")
    assert tl is not None, f"{cls.__name__} does not declare required_trust_level"
    assert tl in (TrustLevel.ANONYMOUS, TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL)
    assert tl == TrustLevel.VERIFIED_EXTERNAL, f"{cls.__name__} must be VERIFIED_EXTERNAL (§5-1)"


# ── TC-06/TC-07: FunctionNode @final gates are non-bypassable ──
def test_tc06_tc07_final_gate_override_raises():
    """Overriding @final _security_gate_input/output raises TypeError at class def."""
    from framework.nodes.function_node import FunctionNode

    with pytest.raises(TypeError):
        class BadNode(FunctionNode):  # noqa: unused
            required_trust_level = TrustLevel.VERIFIED_EXTERNAL

            def _security_gate_input(self, state):  # illegal override
                return state

            def execute(self, state):
                return {}


# ── TC-02: unknown mode -> status=error (SecurityViolation-equivalent handling) ──
def test_tc02_unknown_mode_rejected():
    node = CandidateIntakeNode()
    out = node.execute({"user_input": "hi", "input_context": {"mode": "bogus"}})
    assert out["status"] == AgentStatus.ERROR.value


def test_intake_empty_evaluate_input_rejected():
    node = CandidateIntakeNode()
    out = node.execute({"user_input": "", "input_context": {"mode": "evaluate"}})
    assert out["status"] == AgentStatus.ERROR.value


def test_intake_size_ceiling():
    node = CandidateIntakeNode()
    big = {"user_input": "x" * 300_000}
    gated = node._extra_security_gate_input(big)
    assert gated["status"] == AgentStatus.ERROR.value


# ── Population threshold degradation (domain non-negotiable) ──
def test_population_below_threshold_degrades_no_score():
    node = PotentialAssessmentNode(population_threshold=20)
    # FakeKB (conftest) seeds rare_role cohort=3 < 20.
    out = node.execute({
        "job_category": "rare_role",
        "fit_findings": [{"axis": "skill", "evidence": [{"source": "resume", "quote": "x"}]}],
        "degraded_flags": [],
    })
    assert "cohort_below_threshold" in out["degraded_flags"]
    for a in out["axis_assessments"]:
        assert a["sufficiency"] in ("high", "medium", "low", "unknown")
        assert "overall_score" not in a and "score" not in a  # NO composite score


def test_population_above_threshold_no_degradation():
    node = PotentialAssessmentNode(population_threshold=20)
    out = node.execute({
        "job_category": "engineer",  # cohort=42 >= 20
        "fit_findings": [{"axis": "skill", "evidence": [{"source": "resume", "quote": "x"}]}],
        "degraded_flags": [],
    })
    assert "cohort_below_threshold" not in out["degraded_flags"]


# ── Every generated question carries a targets_concern reference id ──
def test_questions_carry_concern_reference():
    from src.nodes.inner_assess_nodes import QuestionGenerationNode
    node = QuestionGenerationNode()
    out = node.execute({"concern_flags": [{"concern_id": "c-01", "description": "verify skill"}]})
    for q in out["interview_questions"]:
        assert q["targets_concern"] == "c-01"


# ── Feedback partial update is idempotent + missing case_id errors ──
def test_feedback_missing_case_id_errors():
    node = FeedbackUpdateNode()
    out = node.execute({"case_id": "", "validated_payload": {"hire_outcome": "hired"}})
    assert out["status"] == AgentStatus.ERROR.value


def test_feedback_unknown_case_id_errors():
    from src.services.db_service import get_db
    node = FeedbackUpdateNode()
    out = node.execute({"case_id": "nope", "validated_payload": {"hire_outcome": "hired"}})
    assert out["status"] == AgentStatus.ERROR.value
    assert get_db() is not None  # db wired, but case_id absent


def test_case_insert_db_failure_degrades():
    from src.services.db_service import FakeDB, set_db
    set_db(FakeDB(fail_insert=True))
    node = CaseInsertNode()
    out = node.execute({
        "report_draft": {"human_review": {"status": "confirmed"}},
        "job_category": "engineer", "axis_assessments": [], "concern_flags": [],
        "fit_findings": [], "interview_questions": [], "degraded_flags": [],
    })
    assert out["status"] == AgentStatus.SUCCESS.value  # evaluation still returned
    assert "case_insert_failed" in out["degraded_flags"]
    assert out["case_id"] == ""

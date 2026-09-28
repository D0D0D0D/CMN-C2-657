"""Integration tests — full outer graph, 3 modes, HITL round-trips.

Covers docs/03_test_spec.md: evaluate→HITL→resume→success, non-assertive output
contract, ingest, feedback, degradation, TC-05 audit logging.
"""

import json
import logging


from framework.schemas.agent_status import AgentStatus


def _evaluate_payload(job="engineer"):
    return json.dumps({
        "job_category": job, "phase": "post_interview",
        "job_experience_text": "Led the backend team and built the payment service",
        "interview_response_text": "I designed the retry and idempotency logic",
    })


def test_evaluate_suspends_at_hitl(make_graph, ctx_verified):
    g = make_graph()
    r = g.invoke(_evaluate_payload(), ctx=ctx_verified,
                 input_context={"mode": "evaluate", "job_category": "engineer"})
    assert r["status"] == AgentStatus.AWAITING_HUMAN.value


def test_hitl_resume_to_success_produces_non_assertive_report(make_graph, ctx_verified):
    """CRITICAL: resume must REACH success (catches subgraph-caching infinite loop)."""
    g = make_graph()
    r = g.invoke(_evaluate_payload(), ctx=ctx_verified,
                 input_context={"mode": "evaluate", "job_category": "engineer"})
    assert r["status"] == AgentStatus.AWAITING_HUMAN.value

    r2 = g.resume(thread_id=ctx_verified.thread_id, feedback={"action": "approve"})
    assert r2["status"] == AgentStatus.SUCCESS.value

    report = r2["output"]
    # non-assertive contract: NO verdict / recommendation / composite score fields.
    blob = json.dumps(report)
    for forbidden in ("recommendation", "pass_fail", "overall_score", "composite_score", "ranking"):
        assert f'"{forbidden}"' not in blob
    assert report["meta"]["authorship"] == "ai_draft"
    assert report["human_review"]["status"] in ("confirmed", "edited")
    assert report["disclaimers"]
    assert report["case_id"].startswith("cc-")
    # per-axis sufficiency only
    for a in report["potential_assessment"]:
        assert a["sufficiency"] in ("high", "medium", "low", "unknown")


def test_hitl_request_revision_loops_then_approves(make_graph, ctx_verified):
    g = make_graph()
    r = g.invoke(_evaluate_payload(), ctx=ctx_verified,
                 input_context={"mode": "evaluate", "job_category": "engineer"})
    assert r["status"] == AgentStatus.AWAITING_HUMAN.value
    # request-revision keeps us awaiting (regeneration → re-review)
    r2 = g.resume(thread_id=ctx_verified.thread_id,
                  feedback={"action": "correct", "directive": "技術面に寄せて"})
    assert r2["status"] == AgentStatus.AWAITING_HUMAN.value
    # then approve -> success
    r3 = g.resume(thread_id=ctx_verified.thread_id, feedback={"action": "approve"})
    assert r3["status"] == AgentStatus.SUCCESS.value


def test_population_below_threshold_degrades_in_report(make_graph, ctx_verified):
    g = make_graph()
    g.invoke(_evaluate_payload(job="rare_role"), ctx=ctx_verified,
             input_context={"mode": "evaluate", "job_category": "rare_role"})
    r = g.resume(thread_id=ctx_verified.thread_id, feedback={"action": "approve"})
    report = r["output"]
    # sample-too-small stated in evidence; still no composite score.
    stats = json.dumps(report, ensure_ascii=False)
    assert "母集団不足" in stats
    assert '"overall_score"' not in stats


def test_ingest_mode(make_graph, ctx_verified):
    g = make_graph()
    payload = json.dumps({"job_category": "engineer",
                          "rows": [{"job_category": "engineer", "experience_years": 5,
                                    "skill_tags": ["python"], "hire_outcome": "hired",
                                    "performance_score": 4.5}]})
    r = g.invoke(payload, ctx=ctx_verified,
                 input_context={"mode": "ingest", "job_category": "engineer"})
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r["output"]["ingest_summary"]["normalized_count"] == 1


def test_feedback_mode_partial_update(make_graph, ctx_verified):
    from src.services.db_service import FakeDB, set_db
    db = FakeDB()
    db.insert_case({"case_id": "cc-known", "job_category": "engineer", "hire_outcome": "pending"})
    set_db(db)
    g = make_graph()
    payload = json.dumps({"case_id": "cc-known", "hire_outcome": "hired"})
    r = g.invoke(payload, ctx=ctx_verified,
                 input_context={"mode": "feedback", "case_id": "cc-known"})
    assert r["status"] == AgentStatus.SUCCESS.value
    assert db.get_case("cc-known")["hire_outcome"] == "hired"


def test_feedback_unknown_case_id_errors(make_graph, ctx_verified):
    g = make_graph()
    payload = json.dumps({"case_id": "nope", "hire_outcome": "hired"})
    r = g.invoke(payload, ctx=ctx_verified,
                 input_context={"mode": "feedback", "case_id": "nope"})
    assert r["status"] == AgentStatus.ERROR.value


def test_tc05_audit_events_emitted(make_graph, ctx_verified, caplog):
    """TC-05 / S-4: domain emit_trace_event() fires during a run."""
    with caplog.at_level(logging.INFO, logger="agentcore.audit"):
        g = make_graph()
        g.invoke(_evaluate_payload(), ctx=ctx_verified,
                 input_context={"mode": "evaluate", "job_category": "engineer"})
    events = " ".join(r.message for r in caplog.records)
    for evt in ("intake_completed", "input_parsed", "fit_analyzed",
                "potential_assessed", "verification_extracted", "questions_generated"):
        assert evt in events, f"expected domain audit event '{evt}' not emitted"

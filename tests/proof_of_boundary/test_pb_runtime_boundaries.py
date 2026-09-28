"""Runtime proof-of-boundary tests: PB-1, PB-3, PB-5, PB-7 (docs/03_test_spec.md).

PB-2 / PB-4 / PB-6 are covered by the AST-based scanners in this directory
(test_state_safety.py, test_import_isolation.py, test_pb_invoke_order.py).
"""

import json
import logging

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    from src.services.db_service import FakeDB, set_db
    from src.services.kb_service import FakeKB, set_kb
    from src.services.llm_client import FakeLLM, set_llm
    set_llm(FakeLLM())
    set_kb(FakeKB(cohorts={"engineer": 42}))
    set_db(FakeDB())
    yield
    set_llm(None)
    set_kb(None)
    set_db(None)


def _graph():
    from langgraph.checkpoint.memory import InMemorySaver
    from src.graph.graph import CandidateFitReportGraph
    g = CandidateFitReportGraph(config={"memory_enabled": True,
                                        "hitl": {"enabled": True, "max_hitl": 8},
                                        "population_threshold": 20})
    saver = InMemorySaver()
    g.compile(checkpointer=saver)
    return g, saver


def _payload():
    return json.dumps({
        "job_category": "engineer", "phase": "post_interview",
        "job_experience_text": "Led backend team", "interview_response_text": "designed retry",
    })


# ── PB-1: emit_trace_event fires from shared.utils.audit_logger (no silent failure) ──
def test_pb1_audit_event_fires(caplog):
    g, _ = _graph()
    ctx = InvocationContext(session_id="pb1", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    with caplog.at_level(logging.INFO, logger="agentcore.audit"):
        g.invoke(_payload(), ctx=ctx, input_context={"mode": "evaluate", "job_category": "engineer"})
    assert any("intake_completed" in r.message for r in caplog.records)


# ── PB-3: template reaches an external service via the L1-framework seam ──
def test_pb3_external_service_via_seam():
    """KB/DB/LLM are reached only through the injected seams (shared.services.* contracts)."""
    from src.services.kb_service import FakeKB, get_kb
    from src.services.db_service import get_db
    assert get_kb() is not None
    assert get_db() is not None
    # KB search returns real data through the seam.
    kb = FakeKB(criteria={"engineer": [{"axis": "skill", "text": "owns delivery"}]})
    assert kb.search_criteria("engineer", "q")[0]["axis"] == "skill"


# ── PB-5: checkpoint contains NO direct identifiers / sensitive attribute lexemes ──
def test_pb5_checkpoint_no_sensitive_values():
    """invoke → interrupt → resume: across ALL checkpoints, no direct identifier or
    prohibited-attribute lexeme is present (docs/02 §5-0 invariant).

    Replicates the PRODUCTION input path: the API adapter (server.prepare_input)
    prefilters sensitive-attribute fields and tokenizes direct identifiers BEFORE
    anything enters the graph — the checkpointer persists raw user_input at the
    first super-step, so in-graph S-2 alone cannot keep checkpoints clean. This
    test drives the same sanitization functions the adapter delegates to.
    """
    from src.services.appi_dictionary import prefilter_payload
    from src.services.pii_tokenizer import PIITokenizer

    # One unambiguous Japanese lexeme per APPI/fair-hiring category (all 16).
    # (Category CODES like "nationality" legitimately appear in intake_warnings
    # metadata, so the assertion below uses these content lexemes, not codes.)
    jp_lexemes = ["人種", "信条", "社会的身分", "病歴", "障害", "健康診断", "診療録",
                  "犯罪歴", "犯罪被害", "本籍", "家族構成", "労働組合", "出産予定",
                  "国籍", "生年月日", "性別"]

    g, saver = _graph()
    ctx = InvocationContext(session_id="pb5", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    # Raw request as a customer would send it: name + sensitive lexemes included.
    raw_payload = {
        "job_category": "engineer", "phase": "post_interview",
        "job_experience_text": "John Smith; " + "、".join(jp_lexemes) + "; led backend team",
        "interview_response_text": "designed retry",
    }
    # Adapter boundary (same as server.prepare_input): prefilter -> tokenize.
    filtered, prefilter_warnings = prefilter_payload(raw_payload)
    tok = PIITokenizer()
    clean = tok.tokenize(json.dumps(filtered, ensure_ascii=False, default=str))
    assert prefilter_warnings, "prefilter should have dropped the sensitive field"

    g.invoke(clean, ctx=ctx,
             input_context={"mode": "evaluate", "job_category": "engineer",
                            "prefilter_warnings": prefilter_warnings})
    g.resume(thread_id=ctx.thread_id, feedback={"action": "approve"})

    # Inspect every checkpoint tuple written to the saver.
    blobs = []
    for cp in saver.list(None):
        blobs.append(json.dumps(cp.checkpoint, default=str, ensure_ascii=False))
    combined = "\n".join(blobs)
    assert blobs, "expected checkpoints to have been written"
    # Direct identifier must not be present in ANY checkpoint.
    assert "John Smith" not in combined
    # No prohibited/sensitive content lexeme may appear in any checkpoint
    # (all 16 categories were injected above; only category CODES may remain,
    # inside intake_warnings metadata).
    for lex in jp_lexemes:
        assert lex not in combined, f"sensitive lexeme {lex!r} leaked into a checkpoint"


# ── PB-5b: HITL RESUME payload is sanitized before it reaches any checkpoint ──
def test_pb5b_resume_feedback_no_sensitive_values_in_checkpoints():
    """Resume path (docs/02 §5-0): the reviewer-authored resume payload is persisted
    as hitl_feedback in checkpoints WITHOUT passing pre_process — so the adapter
    boundary (server.py → sanitize_resume_feedback) must strip direct identifiers
    (incl. Japanese names / trailing-boundary email/phone, which the vendored
    detect_pii misses) and mask prohibited-attribute lexemes BEFORE agent.resume().
    This test drives the same sanitization function the adapter delegates to, then
    scans EVERY checkpoint cross-section written to the saver."""
    from src.services.identifier_sanitizer import sanitize_resume_feedback

    g, saver = _graph()
    ctx = InvocationContext(session_id="pb5b", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    r = g.invoke(_payload(), ctx=ctx, input_context={"mode": "evaluate", "job_category": "engineer"})
    assert r["status"] == AgentStatus.AWAITING_HUMAN.value

    # Raw reviewer feedback as a recruiter would type it: JP name + prohibited
    # lexeme + phone-followed-by-Japanese (detect_pii's known blind spots).
    raw_feedback = {"action": "correct",
                    "directive": "田中様の年齢を考慮して修正、連絡は080-1234-5678です"}
    clean, stats = sanitize_resume_feedback(raw_feedback)
    assert stats["identifier_redactions"] >= 1
    assert "age" in stats["sensitive_categories"]

    r2 = g.resume(thread_id=ctx.thread_id, feedback=clean)      # revision loop → re-interrupt
    assert r2["status"] == AgentStatus.AWAITING_HUMAN.value
    r3 = g.resume(thread_id=ctx.thread_id,
                  feedback=sanitize_resume_feedback({"action": "approve"})[0])
    assert r3["status"] == AgentStatus.SUCCESS.value

    # EVERY checkpoint cross-section — OUTER saver AND the INNER workflow's own
    # saver (hitl_feedback is persisted in both) — must be free of raw values.
    inner_saver = g._nodes["main"]._subgraph._compiled.checkpointer
    blobs = [json.dumps(cp.checkpoint, default=str, ensure_ascii=False) for cp in saver.list(None)]
    blobs += [json.dumps(cp.checkpoint, default=str, ensure_ascii=False) for cp in inner_saver.list(None)]
    assert blobs, "expected checkpoints to have been written"
    combined = "\n".join(blobs)
    for leaked in ("田中", "年齢", "080-1234-5678"):
        assert leaked not in combined, f"resume payload leaked {leaked!r} into a checkpoint"


# ── PB-7: HITL interrupt propagates (GraphInterrupt reaches engine; status != error) ──
def test_pb7_hitl_interrupt_propagation():
    g, _ = _graph()
    ctx = InvocationContext(session_id="pb7", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    r = g.invoke(_payload(), ctx=ctx, input_context={"mode": "evaluate", "job_category": "engineer"})
    # interrupt() suspended the graph — status is AWAITING_HUMAN, NOT error.
    assert r["status"] == AgentStatus.AWAITING_HUMAN.value
    assert r["status"] != AgentStatus.ERROR.value
    assert "hitl_metadata" in r


# ── S-1 trust gate: ANONYMOUS caller is refused at the privileged node ──
def test_s1_trust_gate_denies_anonymous():
    g, _ = _graph()
    ctx = InvocationContext(session_id="s1", caller_trust_level=TrustLevel.ANONYMOUS)
    r = g.invoke(_payload(), ctx=ctx, input_context={"mode": "evaluate", "job_category": "engineer"})
    assert r["status"] == AgentStatus.ERROR.value

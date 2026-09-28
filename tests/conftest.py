"""Shared test fixtures — inject fake LLM/KB/DB seams (no real network)."""

import json

import pytest

from src.services.db_service import FakeDB, set_db
from src.services.kb_service import FakeKB, set_kb
from src.services.llm_client import FakeLLM, set_llm


@pytest.fixture(autouse=True)
def wire_fakes():
    """Wire deterministic fakes for every test (no network, no real secrets)."""
    set_llm(FakeLLM())
    set_kb(FakeKB(cohorts={"engineer": 42, "rare_role": 3}))
    set_db(FakeDB())
    yield
    set_llm(None)
    set_kb(None)
    set_db(None)


@pytest.fixture
def make_graph():
    """Factory that builds + compiles an outer graph with an InMemorySaver checkpointer."""
    from langgraph.checkpoint.memory import InMemorySaver

    from src.graph.graph import CandidateFitReportGraph

    def _make(config=None):
        cfg = {"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8},
               "population_threshold": 20}
        if config:
            cfg.update(config)
        g = CandidateFitReportGraph(config=cfg)
        g.compile(checkpointer=InMemorySaver())
        return g

    return _make


@pytest.fixture
def ctx_verified():
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    return InvocationContext(session_id="test-session",
                             caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


@pytest.fixture
def agent_config() -> dict:
    # tests/integration/test_graph_flow.py:22-25 の evaluate happy path と同じ追加 config（無し）。
    return {}


@pytest.fixture
def case_payload() -> str:
    # tests/integration/test_graph_flow.py:22-25 の evaluate happy path と同じ候補者入力。
    return json.dumps({
        "job_category": "engineer",
        "phase": "post_interview",
        "job_experience_text": "Led the backend team and built the payment service",
        "interview_response_text": "I designed the retry and idempotency logic",
    })


@pytest.fixture
def live_input_context() -> dict:
    # tests/integration/test_graph_flow.py:22-25 の evaluate happy path と同じ input_context。
    return {"mode": "evaluate", "job_category": "engineer"}


@pytest.fixture
def live_llm_injector():
    # tests/integration/test_graph_flow.py:22-25 の evaluate happy path に実 LLM を配線する。
    def _inject(agent, llm):
        set_llm(llm)
        return agent

    return _inject

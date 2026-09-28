# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# PB-7: HITL Interrupt Propagation.
#
# CONDITIONAL (2026-07-14 ruling): the file is required by exact name for
# every template, but its assertions run only when config/config.yaml declares
# hitl.enabled: true. This template DOES enable HITL, so the assertions below
# execute for real — they are not skipped.
#
# What PB-7 proves:
#   interrupt() raised inside a node's execute() produces a GraphInterrupt
#   (a GraphBubbleUp subclass) that must propagate through BaseNode.__call__()
#   WITHOUT being swallowed by the generic `except Exception` error boundary.
#   A swallowed interrupt would surface as status=error with a traceback in
#   error_log; the correct outcome is status=AWAITING_HUMAN.

from __future__ import annotations

import json
import pathlib
import warnings

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

# ---------------------------------------------------------------------------
# Conditional skip — only runs when config/config.yaml has hitl.enabled: true
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)


@pytest.fixture(autouse=True)
def _wire():
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

    g = CandidateFitReportGraph(
        config={
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 8},
            "population_threshold": 20,
        }
    )
    g.compile(checkpointer=InMemorySaver())
    return g


def _payload() -> str:
    return json.dumps(
        {
            "job_category": "engineer",
            "phase": "post_interview",
            "job_experience_text": "Led backend team",
            "interview_response_text": "designed retry",
        }
    )


def test_pb7_hitl_interrupt_propagates() -> None:
    """GraphInterrupt reaches the LangGraph engine; status is NOT error."""
    g = _graph()
    ctx = InvocationContext(session_id="pb7", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    r = g.invoke(_payload(), ctx=ctx, input_context={"mode": "evaluate", "job_category": "engineer"})

    # interrupt() suspended the graph — the signal was NOT caught by the
    # application error boundary inside BaseNode.__call__().
    assert r["status"] == AgentStatus.AWAITING_HUMAN.value
    assert r["status"] != AgentStatus.ERROR.value
    assert "hitl_metadata" in r


def test_pb7_interrupt_not_swallowed_as_error() -> None:
    """A swallowed GraphInterrupt would appear as status=error + traceback in
    error_log. Assert the error boundary did not fire."""
    g = _graph()
    ctx = InvocationContext(session_id="pb7-b", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    r = g.invoke(_payload(), ctx=ctx, input_context={"mode": "evaluate", "job_category": "engineer"})

    assert r["status"] != AgentStatus.ERROR.value
    assert not r.get("error_log"), f"error boundary fired on interrupt: {r.get('error_log')}"

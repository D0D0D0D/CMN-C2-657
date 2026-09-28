"""LLM injection seam (docs/02_design.md §5-5 / §7).

framework.llm does NOT exist in the 1.0.0 wheel; the canonical LLM abstraction is
shared.services.llm.base_llm.BaseLLM. Nodes receive a BaseLLM via constructor
injection (default None → wired at server startup via wire_services()). Tests
inject a FakeLLM so no real network call happens (real connectivity is verified
at STG).

Secrets are resolved via ctx.secrets.require("LLM_API_KEY") at wiring time in the
API adapter — never os.environ, never stored in State.
"""

from __future__ import annotations

from typing import Iterator

from shared.services.llm.base_llm import BaseLLM

# Module-level singleton wired by wire_services() at server startup.
_LLM: BaseLLM | None = None


def set_llm(llm: BaseLLM | None) -> None:
    """Wire the process-wide LLM client (called by server.wire_services())."""
    global _LLM
    _LLM = llm


def get_llm() -> BaseLLM | None:
    """Return the wired LLM client, or None if not wired (degraded mode)."""
    return _LLM


class FakeLLM(BaseLLM):
    """Deterministic fake LLM for tests + local degraded operation.

    Returns canned, evidence-shaped completions so unit/integration tests never
    touch the network. NOT used in production (server.wire_services injects a
    real client). Kept in src/ (not tests/) so degraded local runs also work.
    """

    def __init__(self, canned: str = "structured-analysis-placeholder") -> None:
        self._canned = canned
        self._tools: list = []

    def complete(self, messages: list) -> dict:
        return {
            "content": self._canned,
            "tool_calls": [],
            "model": "fake-llm",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "model": "fake-llm"},
        }

    def stream(self, messages: list) -> Iterator[str]:
        yield self._canned

    def bind_tools(self, tools: list) -> "FakeLLM":
        self._tools = tools
        return self

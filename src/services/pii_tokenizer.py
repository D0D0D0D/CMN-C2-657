"""Request-scoped PII tokenizer — API adapter ONLY (docs/02_design.md §3 / §5-0).

Direct identifiers (name / contact / DOB / address / photo ref) are detected via
shared.security.pii_detector and replaced with deterministic placeholder tokens
BEFORE the text enters the graph. The token→value map lives ONLY in request-scoped
memory in server.py and is NEVER persisted (no DB, no KB, no checkpoint).

CRITICAL (§5-0, sibling-template incident): resolution back to display values
happens ONLY in the API adapter's response shaping (server.py), NEVER inside any
graph node — the checkpointer persists State every super-step, so any in-graph
resolve is a leak. This module exposes tokenize() (adapter → graph) and a pure
resolve() helper the adapter uses on the way out; nodes must not import resolve().
"""

from __future__ import annotations

from shared.security.pii_detector import detect_pii


class PIITokenizer:
    """Deterministic, request-scoped tokenizer. Construct one per request."""

    def __init__(self) -> None:
        # token -> original value (request memory only; discarded when the request ends)
        self._map: dict[str, str] = {}
        self._counter: dict[str, int] = {}

    def tokenize(self, text: str) -> str:
        """Replace detected direct identifiers with deterministic tokens.

        Returns the tokenized text safe to pass into the graph. The reverse map is
        held in this instance and MUST NOT be serialized into State.
        """
        if not text or not isinstance(text, str):
            return text
        findings = detect_pii(text)
        if not findings:
            return text
        # Replace from the end so offsets stay valid.
        out = text
        for f in sorted(findings, key=lambda x: x["start"], reverse=True):
            token = self._token_for(f["type"], f["value"])
            out = out[: f["start"]] + token + out[f["end"]:]
        return out

    def _token_for(self, pii_type: str, value: str) -> str:
        # Reuse the same token for the same value within a request (consistency).
        for tok, orig in self._map.items():
            if orig == value:
                return tok
        label = {
            "name": "CANDIDATE",
            "email": "CONTACT",
            "phone_jp": "CONTACT",
            "phone_us": "CONTACT",
            "ssn_us": "ID",
            "my_number_jp": "ID",
            "credit_card": "ID",
        }.get(pii_type, "PII")
        self._counter[label] = self._counter.get(label, 0) + 1
        token = f"[{label}]" if self._counter[label] == 1 else f"[{label}_{self._counter[label]}]"
        self._map[token] = value
        return token

    def resolve(self, text: str) -> str:
        """Reverse tokens back to display values — API-adapter response shaping ONLY.

        MUST NOT be called from inside any graph node (§5-0). Present here so the
        adapter can optionally restore a display name in the returned report only.
        """
        if not text or not isinstance(text, str):
            return text
        out = text
        for token, orig in self._map.items():
            out = out.replace(token, orig)
        return out

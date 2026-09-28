"""AgentCore Platform v1.0 — CMN-C2-657 API adapter (docs/02_design.md §3).

Standalone HTTP entry point. Adapters are the ONLY place where:
  - PII tokenization happens (BEFORE text enters the graph — §3 / §5-0);
  - reference resolution back to display values happens (AFTER invoke — never in a
    graph node, because the checkpointer persists State every super-step);
  - the graph is compiled with an explicit checkpointer (HITL needs one — §6).

Three routes (/ingest, /invoke, /feedback) invoke the SAME graph with a `mode`.
"""

from __future__ import annotations

import threading
import weakref
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.entry_adapter.auth import _require_verified_trust, _resolve_trust

from src.graph.graph import CandidateFitReportGraph
from src.services.pii_tokenizer import PIITokenizer

NAMESPACE = "cmn"
AGENT_NAME = "cmn_c2_657_candidate_fit_report"

app = FastAPI(title="CandidateFitAndPerformancePotentialReportAgent")

# HITL config passed to the graph so BaseGraph.invoke() attaches the thread config.
_GRAPH_CONFIG = {
    "memory_enabled": True,
    "hitl": {"enabled": True, "max_hitl": 8},
    "population_threshold": 20,
    "checkpoint_ttl_hours": 120,
    "summarize_threshold_chars": 20000,
}

agent = CandidateFitReportGraph(config=_GRAPH_CONFIG)
# EXPLICIT checkpointer — compile() does NOT auto-derive one in this SDK version (§6).
# STG swaps InMemorySaver for a durable saver; the inner graph compiles its own.
agent.compile(checkpointer=InMemorySaver())
agent.provision_secrets(secrets_factory(namespace=NAMESPACE, agent_name=AGENT_NAME))


def wire_services() -> None:
    """Wire real LLM / KB / DB clients at startup.

    A template that skips this 500s on first invoke. Secrets are read via
    ctx.secrets.require() (never os.environ). Real client construction is left as a
    deployment step; the seams (set_llm/set_kb/set_db) are populated here.

    At STG this constructs the real shared.services clients from the required
    secrets (LLM_API_KEY / CANDIDATE_DB_DSN / RECRUIT_KB_URL / RECRUIT_KB_API_KEY).
    Until then it wires deterministic fallbacks so /health and structure checks pass.
    """
    from src.services.db_service import set_db, FakeDB
    from src.services.kb_service import set_kb, FakeKB
    from src.services.llm_client import set_llm, FakeLLM

    # NOTE (STG): replace these with real clients built from ctx.secrets.require(...).
    set_llm(FakeLLM())
    set_kb(FakeKB())
    set_db(FakeDB())


wire_services()


class IngestRequest(BaseModel):
    input: str
    session_id: str = ""


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    job_category: str = ""
    phase: str = ""
    # HITL resume: the client MUST echo `thread_id` from the awaiting_human
    # response of the original /invoke. InvocationContext mints a RANDOM
    # thread_id per instance (it is NOT derived from session_id), so a resume
    # cannot re-locate the suspended checkpoint from session_id alone — without
    # this the resume targets a nonexistent thread and never reaches the
    # waiting interrupt().
    thread_id: str = ""
    resume: dict | None = None


class FeedbackRequest(BaseModel):
    case_id: str
    hire_outcome: str = ""
    performance_score: float | None = None
    evaluator_edited: bool | None = None
    session_id: str = ""


def _ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication.

    Entry auth is implemented by entry_adapter.auth:
      - verified middleware trust (request.state.trust_level) is used as-is;
      - otherwise INVOKE_AUTH_TOKEN Bearer auth promotes ANONYMOUS →
        VERIFIED_EXTERNAL (_resolve_trust raises 401 on token mismatch);
      - an unauthenticated caller (no verified middleware trust AND no token
        promotion) is rejected with 401 — the old fallback that defaulted such
        callers to VERIFIED_EXTERNAL was fail-open and is removed.

    Scope note: the entry_adapter part also exposes a tenant/org
    scope contract, but this template has NO scope model — the KB is keyed by
    job_category, the case store by case_id, and there is no code path that
    reads request tenant/org scope. This template is single-tenant-per-
    deployment: tenant isolation comes from the deployment's own DB/KB
    credentials, NOT from PII tokenization or session (those bound the blast
    radius of one deployment's data, they do not separate tenants). Enforcing
    authenticate_scope() would gate on a scope nothing consumes (STG-default
    theater), so only the auth core (_resolve_trust / _require_verified_trust)
    is wired.
    """
    trust, token_authenticated = _resolve_trust(request)
    # Unauthenticated = trust is still ANONYMOUS after resolution and no token
    # promotion. Test the trust VALUE, not attribute presence — a middleware
    # that explicitly stamps ANONYMOUS must still be treated as unauthenticated
    # (→ 401), not silently accepted as "verified because the attr exists".
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


def _require_resume_ownership(thread_id: str, ctx: InvocationContext) -> None:
    """Fail-closed checkpoint ownership check for HITL resume.

    A resume call replays into an existing checkpoint thread; the same entry
    auth as /invoke is NOT sufficient on its own because any authenticated
    caller could guess/replay another session's thread_id. Verify the awaiting
    checkpoint exists AND belongs to this caller before delivering feedback
    (session_id always; caller_id when the original invoke recorded one).
    """
    compiled = getattr(agent, "_compiled", None)
    if compiled is None:
        raise HTTPException(status_code=409, detail="No suspended session for this thread.")
    snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
    values = getattr(snapshot, "values", None) or {}
    if not values:
        # Unknown thread — fail closed, do not leak whether it ever existed.
        raise HTTPException(status_code=404, detail="No suspended session for this thread.")
    # Fail-CLOSED ownership. session_id is the primary owner key here:
    # token-authenticated callers all share caller_id="" so caller_id ALONE cannot
    # gate. A checkpoint that records NO session_id is therefore un-resumable over
    # HTTP — we refuse rather than let any authenticated caller claim it.
    owner_session = values.get("session_id")
    if not owner_session or owner_session != ctx.session_id:
        raise HTTPException(status_code=403, detail="Session does not own this thread.")
    # caller_id is defense-in-depth: enforce it only when the checkpoint recorded a
    # non-empty owner (verified-middleware deployments), and then it must match.
    owner_caller = values.get("caller_id") or ""
    if owner_caller and owner_caller != (ctx.caller_id or ""):
        raise HTTPException(status_code=403, detail="Caller does not own this thread.")
    # Require a pending HITL wait (replay guard): a completed thread has
    # an empty snapshot.next, so a resume replayed after completion is rejected.
    # propagate_hitl=True → the outer pending checkpoint carries subgraph_thread_id.
    if not getattr(snapshot, "next", None) or not values.get("subgraph_thread_id"):
        raise HTTPException(status_code=409, detail="Thread is not awaiting human input.")


# Per-thread locks serialize the ownership-check + resume for one thread within
# this process (resume TOCTOU guard). Without it two concurrent valid
# resumes can both pass the pending-state check and both replay feedback.
# WeakValueDictionary so entries drop when no request holds the lock. A durable,
# multi-process deployment must replace this with a per-thread lease/CAS.
_RESUME_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_RESUME_LOCKS_GUARD = threading.Lock()


def _resume_lock(thread_id: str) -> threading.Lock:
    with _RESUME_LOCKS_GUARD:
        lock = _RESUME_LOCKS.get(thread_id)
        if lock is None:
            lock = threading.Lock()
            _RESUME_LOCKS[thread_id] = lock
        return lock


def prepare_input(raw_input: str, tokenizer: PIITokenizer) -> tuple[str, list[str]]:
    """Adapter-boundary sanitization BEFORE anything enters the graph (§3 / §5-0).

    1. Sensitive-attribute prefilter: drop JSON fields (or mask free-text lexemes)
       carrying APPI special-care / fair-hiring prohibited content — the checkpointer
       persists raw user_input at the first super-step, so this MUST happen here,
       not only in the in-graph S-2 projection (which remains as defense-in-depth).
    2. PII tokenization: direct identifiers → reference tokens (map stays in
       request-scoped memory only).

    Returns ``(clean_input, prefilter_warnings)``.
    """
    import json as _json

    from src.services.appi_dictionary import mask_sensitive, prefilter_payload

    warnings: list[str] = []
    text = raw_input or ""
    if text.strip().startswith("{"):
        try:
            payload = _json.loads(text)
            filtered, warnings = prefilter_payload(payload)
            text = _json.dumps(filtered, ensure_ascii=False, default=str)
        except _json.JSONDecodeError:
            text, cats = mask_sensitive(text)
            warnings = [f"prefiltered_sensitive:freetext:{c}" for c in cats]
    else:
        text, cats = mask_sensitive(text)
        warnings = [f"prefiltered_sensitive:freetext:{c}" for c in cats]
    return tokenizer.tokenize(text), warnings


def _invoke_with_mode(raw_input: str, mode: str, ctx: InvocationContext, extra_context: dict, tokenizer: PIITokenizer):
    """Sanitize (adapter), invoke with mode via input_context, resolve (adapter)."""
    clean, prefilter_warnings = prepare_input(raw_input, tokenizer)
    input_context = {"mode": mode, "prefilter_warnings": prefilter_warnings, **extra_context}
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(clean, ctx=ctx, input_context=input_context)
    return _resolve_output(result, tokenizer)


def _resolve_output(result: dict, tokenizer: PIITokenizer) -> dict:
    """Reference resolution — API-adapter response shaping ONLY (§5-0).

    Operates on a COPY of the invoke output. Never mutates any graph/checkpoint state.
    """
    out = dict(result)
    report = out.get("output") or out.get("result")
    if isinstance(report, dict):
        md = report.get("markdown")
        if isinstance(md, str):
            report = dict(report)
            report["markdown"] = tokenizer.resolve(md)
            out["output"] = report
    return out


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    tok = PIITokenizer()
    return _invoke_with_mode(req.input, "ingest", ctx, {}, tok)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    tok = PIITokenizer()
    if req.resume is not None:
        # HITL resume: the reviewer-authored payload (revision text etc.) does NOT
        # pass through pre_process and is persisted as hitl_feedback in checkpoints,
        # so it MUST be sanitized HERE — same §5-0 boundary as the initial input
        # (sensitive-lexeme masking + direct-identifier removal, incl. the JP-name /
        # trailing-boundary gaps of detect_pii — see identifier_sanitizer.py).
        from shared.utils.audit_logger import emit_trace_event

        from src.services.identifier_sanitizer import sanitize_resume_feedback

        # The suspended checkpoint lives under the thread_id returned by the
        # original /invoke — NOT under a freshly-minted ctx.thread_id (which is
        # random per InvocationContext). The client MUST echo it back.
        if not req.thread_id:
            raise HTTPException(status_code=422, detail="thread_id is required to resume.")
        clean_feedback, stats = sanitize_resume_feedback(req.resume)
        if stats["identifier_redactions"] or stats["sensitive_categories"]:
            # S-4: counts / category codes only — never the removed content.
            emit_trace_event("resume_feedback_sanitized", stats, {"session_id": ctx.session_id})
        # Serialize check + resume per thread so a concurrent duplicate cannot
        # slip between the pending-state check and agent.resume().
        with _resume_lock(req.thread_id):
            _require_resume_ownership(req.thread_id, ctx)
            with bound_secrets(agent._secrets_provider):
                result = agent.resume(thread_id=req.thread_id, feedback=clean_feedback)
        return _resolve_output(result, tok)
    extra = {"job_category": req.job_category, "phase": req.phase,
             "summarize_threshold_chars": _GRAPH_CONFIG["summarize_threshold_chars"]}
    return _invoke_with_mode(req.input, "evaluate", ctx, extra, tok)


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    import json
    ctx = _ctx(request, req.session_id)
    tok = PIITokenizer()
    payload = {"case_id": req.case_id}
    if req.hire_outcome:
        payload["hire_outcome"] = req.hire_outcome
    if req.performance_score is not None:
        payload["performance_score"] = req.performance_score
    if req.evaluator_edited is not None:
        payload["evaluator_edited"] = req.evaluator_edited
    # feedback carries no candidate free-text; pass the structured payload as input.
    return _invoke_with_mode(json.dumps(payload), "feedback", ctx,
                             {"case_id": req.case_id}, tok)


@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME}

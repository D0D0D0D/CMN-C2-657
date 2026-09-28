"""AgentCore Platform v1.0 — CMN-C2-657 state schemas (docs/02_design.md §2-4)."""

# ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
# object kinds). LangGraph checkpoints use msgpack serialization; non-primitive
# model objects cause silent corruption. All fields below are primitives or JSON-serializable
# containers only.
#
# §5-0 invariant: candidate direct identifiers (name/contact/DOB/address) and
# APPI special-care-required / fair-hiring-prohibited attributes MUST NEVER be
# stored in State — they are tokenized in the API adapter (src/api/server.py)
# and structurally dropped by the allowlist projection in pre_process. Only
# tokenized job-related text and code/numeric fields may transit State for the
# session (bounded by checkpoint_ttl_hours).

# Architecture review finding: the agent-specific keys below may be absent from
# State until the node that writes them has run — including branch-dependent
# keys (`ingest_summary` is written only on the /ingest branch, never on an
# /evaluate run) and error paths that short-circuit before the writer. Declaring
# them as required keys misstates the schema and, after a HITL interrupt, makes
# the restored checkpoint look non-conformant to a total-TypedDict reading.
# NotRequired[...] states the actual contract: present-or-absent, never None-
# padded. Framework-injected keys stay inherited from AgentState unchanged.

from typing import Any, NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Outer graph State — AgentState subtype.

    Shared fields (user_input, status, session_id, node_history, error_log,
    hitl_* etc.) are inherited from AgentState. Only agent-specific fields are
    declared below.
    """

    mode: NotRequired[str]                  # "ingest" | "evaluate" | "feedback"
    job_category: NotRequired[str]          # normalized job category (cohort matching key)
    phase: NotRequired[str]                 # "document" | "pre_interview" | "post_interview"
    case_id: NotRequired[str]               # assessment case id (minted on evaluate / required on feedback)
    assessment_payload: NotRequired[dict]   # inner graph result (citations/concerns/questions — de-sensitized)
    degraded_flags: NotRequired[list]       # degradation info (KB empty / cohort short / DB write failed)
    intake_warnings: NotRequired[list]      # intake warnings (dropped columns / summarization applied)
    # NOTE: validated_input, result, hitl_* are inherited from AgentState.


class InnerState(AgentState):
    """Inner CandidateAssessmentWorkflow State (docs/02_design.md §2-2).

    Subclasses AgentState so it inherits every framework-injected key
    (session_id, correlation_id, trace_id, thread_id, caller_trust_level,
    hitl_feedback / hitl_draft / hitl_status / hitl_metadata, node_history,
    error_log, execution_time, status, …) that BaseGraph.invoke seeds and
    InvocationContext.from_state reads — then adds the inner-specific fields
    below.

    CRITICAL (§2-2, known bug class): the inner routing function and
    get_output() MUST be annotated ``state: InnerState`` — never
    ``state: AgentState``. Annotating with the *parent* AgentState causes
    LangGraph to project the state down to AgentState's keys and silently drop
    every inner-specific field below, so every conditional route falls through
    to the default branch. Annotating with this subclass keeps all keys.
    """

    mode: NotRequired[str]
    job_category: NotRequired[str]
    phase: NotRequired[str]
    case_id: NotRequired[str]
    validated_payload: NotRequired[dict]    # parsed allowlist-projected structured input
    ingest_summary: NotRequired[dict]       # /ingest result (row counts, dropped columns)
    fit_findings: NotRequired[list]         # fit_analysis output (each with evidence)
    axis_assessments: NotRequired[list]     # potential_assessment output (per-axis sufficiency, NO composite score)
    concern_flags: NotRequired[list]        # verification_points output (code + evidence pointer)
    interview_questions: NotRequired[list]  # question_generation output (each carries targets_concern id)
    report_draft: NotRequired[dict]         # assembled report draft presented at HITL
    revision_directive: NotRequired[Any]    # human "request-revision" instruction (routes back to regeneration)
    degraded_flags: NotRequired[list]
    assessment_payload: NotRequired[dict]   # final payload returned to outer merge_output()

# CMN-C2-657 — CandidateFitAndPerformancePotentialReportAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: CMN

## Overview

This agent drafts a recruiting assessment. Given a candidate's job-related material — experience
text, interview notes — it produces evidence-linked fit findings, a per-axis performance-potential
assessment, the concerns that are still unverified, and interview questions written to close those
specific concerns, each with a scoring rubric. A human reviewer then approves or corrects the
draft; the report is not final until they do.

It is designed for the ten minutes after an interview, the document-screening stage, and the
after-the-fact loop where hiring and performance outcomes are registered so the organization's own
picture of "what a successful hire looks like here" accumulates as anonymized statistics.

What the design refuses to do is as much the point as what it does:

- **There is no hire/no-hire output.** The report has no decision field, no recommendation and no
  composite score — structurally, not by convention. Per-axis sufficiency is `high` / `medium` /
  `low` / `unknown`. Thresholding those values to auto-screen candidates is a misuse of the
  template.
- **Protected and special-care attributes are removed before anything enters the graph.** The
  entry adapter runs an allow-list projection and a category dictionary over the payload —
  covering the special-care categories of Japan's APPI (race, creed, social status, medical
  history, disability, health-check and treatment records, criminal record and victimhood) plus
  fair-hiring prohibited attributes (registered domicile, family circumstances, union membership,
  marriage/childbirth plans, nationality, age, gender). Dropped fields are recorded as a warning
  code only; the content itself is never logged. This happens in the adapter rather than only in
  the in-graph gate because the checkpointer persists the raw input at the first super-step — an
  in-graph check would be too late.
- **Direct identifiers are tokenized at the boundary.** Names and contact details become reference
  tokens before the graph sees them and are resolved back only when the response is shaped. The
  token map lives in request-scoped memory and is never persisted, so the case store keeps job
  category, experience band and evaluation codes — not people.
- **Human review is mandatory, and so is sanitizing what the reviewer writes back.** The reviewer's
  correction text does not pass through the input node and is persisted as HITL feedback in
  checkpoints, so it is masked and de-identified at the same adapter boundary as the original
  input.
- **A thin population is stated, not smoothed over.** Below `population_threshold` (default 20)
  the statistical backing is reported as weak rather than presented as a finding.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

🔴 **The shipped `src/api/server.py` wires deterministic fakes, not real services.** Its
`wire_services()` calls `set_llm(FakeLLM())`, `set_kb(FakeKB())` and `set_db(FakeDB())` so the
structure runs end to end without any backend. Replacing those three seams with real clients built
from `ctx.secrets.require(...)` is the first integration step, and until it is done every report is
produced by a stub.

Provide:

- an AgentCore runtime with a checkpointer. HITL requires one, and `compile()` does not derive one
  in this SDK version — the standalone server passes an explicit in-memory saver, which a real
  deployment must replace with a durable one. An in-memory saver loses every suspended review on
  restart, and the per-thread resume lock is process-local, so a multi-process deployment needs a
  per-thread lease or compare-and-set instead;
- a language model behind `set_llm()`, an organization hiring-criteria knowledge base behind
  `set_kb()`, and a candidate-case store behind `set_db()`, all inside your own tenant;
- caller authentication in front of the entry adapter.

`config/agent.yaml` declares `LLM_API_KEY` under `requires.secrets`. The knowledge-base and
case-store credentials a real deployment needs (`CANDIDATE_DB_DSN`, `RECRUIT_KB_URL`,
`RECRUIT_KB_API_KEY`) are named in `wire_services()` as the intended integration point but are
**not** declared in the manifest — add them there when you wire the real clients, so a deployment
missing one fails closed at compile time.

Runtime parameters are in `config/config.yaml`, but note what actually reads them: the standalone
server does not load that file. It passes a hardcoded `_GRAPH_CONFIG` dict, so changing the file
alone does not change the standalone server's behaviour.

| Key | Read by |
|---|---|
| `memory_enabled`, `hitl.enabled`, `hitl.max_hitl` (`8`) | Passed by the server; the framework attaches the HITL thread configuration |
| `population_threshold` (`20`) | The performance-potential comparison — below it the report degrades explicitly |
| `checkpoint_ttl_hours` (`120`) | Retention of a review-waiting session |
| `summarize_threshold_chars` (`20000`) | Long-input summarization threshold, forwarded per invocation |
| `max_retry` (`3`), `timeout_seconds` (`120`) | Not in the server's `_GRAPH_CONFIG`; the framework's own defaults apply |
| `retention_months` (`60`) | **Not read by any code path.** Case retention is a deployment/operations responsibility, not something this value enforces |

## Input

Three routes drive the same graph with a different `mode`:

| Route | Mode | Body |
|---|---|---|
| `POST /invoke` | `evaluate` | `input` (candidate payload, JSON string or free text), `session_id`, `job_category`, `phase`; on resume, `thread_id` and `resume` |
| `POST /ingest` | `ingest` | `input`, `session_id` — historical interview and evaluation records |
| `POST /feedback` | `feedback` | `case_id`, `hire_outcome`, `performance_score`, `evaluator_edited`, `session_id` — outcome registered after the fact |

Callers must be authenticated on every route. The entry adapter uses a verified middleware trust
level when present; otherwise, if `INVOKE_AUTH_TOKEN` is set on the server environment, a matching
`Authorization: Bearer` header promotes the caller to `VERIFIED_EXTERNAL`. A caller that is neither
middleware-verified nor token-authenticated is rejected with **401**.

```bash
curl -X POST http://localhost:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"job_category\":\"engineer\",\"job_experience_text\":\"…\",\"interview_response_text\":\"…\"}",
    "job_category": "engineer",
    "session_id": "demo-001"
  }'
```

The first call suspends for review and returns `status: awaiting_human` together with a
`thread_id`. **The client must echo that `thread_id` back to resume** — the suspended checkpoint
lives under it, and it is not derivable from `session_id` (each invocation context mints a random
one). Resuming without it is a **422**.

```bash
curl -X POST http://localhost:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{"input": "", "session_id": "demo-001",
       "thread_id": "<thread_id from the awaiting_human response>",
       "resume": {"action": "approve"}}'
```

`{"action": "correct", "directive": "…"}` regenerates against the instruction and returns for
review again, up to `hitl.max_hitl` rounds. Resume is ownership-checked and fails closed: an
unknown thread is **404**, a thread owned by another session is **403**, a thread that is not
actually waiting — including one already completed — is **409**, and a checkpoint that recorded no
`session_id` is un-resumable over HTTP by design.

## Output

The first `/invoke` returns the suspend envelope:

```json
{
  "status": "awaiting_human",
  "thread_id": "…",
  "hitl_metadata": {"reason": "human review required"},
  "output": "",
  "trace_id": "…", "correlation_id": "…",
  "node_history": ["InitializeNode", "CandidateIntakeNode"],
  "error_log": []
}
```

After an approving resume, `status` is `success` and `output` is the report object, carrying
`case_id`, `meta`, `fit_analysis`, `potential_assessment`, `verification_points`,
`interview_questions`, `similar_cases`, `human_review`, `disclaimers` and a rendered `markdown`
view. Only `markdown` has its reference tokens resolved back to display values, and only on the
response copy — graph and checkpoint state keep the tokenized form.

Values are illustrative; real output depends on the candidate material, the knowledge base and the
accumulated case statistics.

## Security and Limitations

Every node requires verified-external trust, so the framework's trust gate stops an insufficiently
trusted caller before any node body runs — the adapter's 401 is the first of two independent
checks. Sensitive-attribute filtering and identifier tokenization both happen in the adapter,
ahead of the checkpoint boundary, and the same treatment is applied to reviewer feedback on the
resume path.

Known limits, stated plainly:

- **This is not a hiring decision, and must not be wired into one.** The output is a draft of
  observations, evidence and open questions. Automating a screen from it defeats the design.
- **Dictionary coverage is a deployment responsibility.** The special-care and prohibited-attribute
  detection is a category-based lexicon; have your own legal function review its completeness
  before use.
- **Statistical backing is weak until data accumulates.** Fit analysis and question generation work
  from day one; the successful-employee comparison degrades explicitly below the population
  threshold.
- **Because protected attributes are never stored, attribute-level fairness monitoring is not
  possible.** That is a deliberate trade — exclusion at the source over after-the-fact
  monitoring — and it is recorded as such in `docs/02_design.md` rather than left implicit.
- **Employment-related AI regulation may apply.** Deploying this where such rules bind (the EU AI
  Act among them) requires a legal review first; the template does not perform a conformity
  assessment.
- Japanese is the primary language; English input is accepted.

Output is machine-generated and assistive. It does not replace a human assessment, and the final
judgement about a person must be made by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2)
- Generation mode: `llm`
- HITL: **enabled and mandatory** (`hitl.max_hitl: 8`)
- Persistence: checkpointer required; the standalone server uses an in-memory saver
- Required trust level: `VERIFIED_EXTERNAL`

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is built to run on the platform, which supplies the secret provider, a durable checkpointer and the entry authentication in front of it. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not fall back to a degraded
mode when the platform is absent — but it does not refuse to start either. The standalone server
compiles the graph with an in-memory checkpointer, wires the fake LLM / knowledge-base / case-store
seams and provisions its secret provider at import; `/health` returns `{"status": "ok"}`. With
`INVOKE_AUTH_TOKEN` set and a valid bearer token, the whole flow runs — suspend, resume, report —
against those fakes, which is what the test suite exercises. The content of such a report is a stub
and means nothing about a real candidate. Without `INVOKE_AUTH_TOKEN`, every request is **401**.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the adapter-boundary sanitization
design, the checkpoint-safety argument and the fairness trade-off. `docs/03_test_spec.md` records
the test specification and the verification results, including the HITL resume boundary tests.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

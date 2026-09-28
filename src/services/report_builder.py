"""Report assembly (docs/02_design.md §2-5 / §11-2).

Builds the candidate assessment report as structured JSON + a meeting-ready
Markdown rendering. The output schema is NON-ASSERTIVE BY CONSTRUCTION:
  - no pass/fail, recommendation, hire, or overall/composite score field EXISTS;
  - only per-axis sufficiency (high/medium/low/unknown) + evidence;
  - authorship="ai_draft", a human_review block, and disclaimers are mandatory.

This builder never emits a forbidden key; assertive_guard.scan_forbidden_keys is
the belt-and-braces S-3 check applied afterwards.
"""

from __future__ import annotations

from datetime import datetime, timezone

DISCLAIMERS = [
    "本レポートは意思決定支援のドラフトであり、採用可否の判断・確定は人間が行う。",
    "全ての所見は入力記述の引用または匿名統計に基づく。裏付け（evidence）のない所見は sufficiency:unknown とする。",
    "生成された面接設問は禁止質問の自動判定を行っていない。面接官が最終確認すること。",
]


def build_report(
    *,
    case_id: str,
    job_category: str,
    phase: str,
    kb_coverage: str,
    fit_findings: list,
    axis_assessments: list,
    concern_flags: list,
    interview_questions: list,
    similar_cases: list | None = None,
    human_review: dict | None = None,
) -> dict:
    """Assemble the structured report dict (no verdict / no composite score)."""
    report: dict = {
        "case_id": case_id,
        "meta": {
            "job_category": job_category,
            "phase": phase,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "kb_coverage": kb_coverage,          # "full" | "degraded"
            "authorship": "ai_draft",            # AI-draft marker (mandatory)
        },
        "fit_analysis": fit_findings,
        "potential_assessment": axis_assessments,   # per-axis sufficiency only, NO score
        "verification_points": concern_flags,
        "interview_questions": interview_questions,  # each carries targets_concern id
        "similar_cases": similar_cases or [],
        "human_review": human_review or {"status": "pending", "edited_sections": []},
        "disclaimers": list(DISCLAIMERS),
    }
    report["markdown"] = _render_markdown(report)
    return report


def _render_markdown(report: dict) -> str:
    """Meeting-ready Markdown: axis findings → concerns → next-phase questions."""
    lines: list[str] = []
    meta = report["meta"]
    lines.append(f"# 候補者評価ドラフト（AI起草）— {meta['job_category']} / {meta['phase']}")
    lines.append("")
    lines.append(f"- case_id: `{report['case_id']}`  |  KB: {meta['kb_coverage']}  |  authorship: {meta['authorship']}")
    lines.append(f"- human_review: {report['human_review'].get('status')}")
    lines.append("")
    lines.append("## ① 適合性（根拠付き）")
    for f in report["fit_analysis"]:
        lines.append(f"- **{f.get('axis','?')}** [{f.get('alignment','unknown')}] {f.get('note','')}")
    lines.append("")
    lines.append("## ② 活躍可能性（軸別・総合点なし）")
    for a in report["potential_assessment"]:
        lines.append(f"- **{a.get('axis','?')}**: 充足度 {a.get('sufficiency','unknown')} / 懸念 {a.get('concerns',[])}")
    lines.append("")
    lines.append("## ③ 検証論点")
    for c in report["verification_points"]:
        lines.append(f"- `{c.get('concern_id','?')}`: {c.get('description','')} — 未検証理由: {c.get('why_unverified','')}")
    lines.append("")
    lines.append("## ④ 次フェーズ検証設問")
    for q in report["interview_questions"]:
        lines.append(f"- `{q.get('question_id','?')}` (→{q.get('targets_concern','?')}): {q.get('question','')}")
    lines.append("")
    lines.append("---")
    for d in report["disclaimers"]:
        lines.append(f"> {d}")
    return "\n".join(lines)

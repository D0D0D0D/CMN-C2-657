# Test Specification — CMN-C2-657 CandidateFitAndPerformancePotentialReportAgent

> テスト仕様。docs/02_design.md（§5-0 PB 仕様・§13 Open Items OI-1）に基づく。
> 実行系: pytest（`.venv` / agenticstar-agentcore==1.0.0）。LLM/KB/DB は全て注入シームで
> fake 化（`tests/conftest.py`）— テストは実ネットワークに一切出ない（実接続確認は STG 環境）。

## Test Strategy
- Coverage target: 全 TC / PB / ドメイン固有 TC 100% pass（数値カバレッジは CI `run-tests` の coverage 出力を参照）
- Test types: Unit（services / nodes）/ Integration（外側グラフ 3 モード + HITL 往復）/ Proof-of-Boundary

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass（`State`/`InnerState` とも `AgentState` 継承 TypedDict） | ✅ pass（`test_state_safety.py` AST scan） |
| TC-02 | 不正入力で拒否（unknown mode / empty input / size ceiling） | `status="error"`＋理由（SecurityViolationError 相当のゲート拒否） | ✅ pass（`test_nodes.py::test_tc02_unknown_mode_rejected` ほか） |
| TC-03 | No JWT/Credential in State | CI の資格情報スキャン: 0 violations | ✅ pass（CI gate + `test_state_safety.py`） |
| TC-04 | InvocationContext via configurable only | State 非格納（`InvocationContext.from_state` 経由のみ） | ✅ pass（`test_state_safety.py` PROHIBITED_TYPE_ANNOTATIONS scan） |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` absent from `execute()` body | ✅ 0 duplicates（全ノード grep 確認・ドメインイベントのみ emit） |
| TC-06 | S-2: `_security_gate_input()` not overridden (FunctionNode subclass) | `TypeError` at class definition if overridden | ✅ pass（`test_nodes.py::test_tc06_tc07_final_gate_override_raises`） |
| TC-07 | S-3: `_security_gate_output()` not overridden (FunctionNode subclass) | 同上 | ✅ pass（同上・`@final` __init_subclass__ 実機確認） |
| TC-08 | `required_trust_level` enforced | 全ノード `VERIFIED_EXTERNAL` 明示宣言＋ANONYMOUS 呼出は拒否 | ✅ pass（`test_nodes.py::test_tc08_trust_level_declared` + `test_pb_runtime_boundaries.py::test_s1_trust_gate_denies_anonymous`） |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial | `CandidateIntakeNode`: 入力サイズ上限を state 経由で拒否（raise しない契約遵守） | ✅ pass（`test_nodes.py::test_intake_size_ceiling`） |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial | `ReportComposeNode`: 断定文言降格＋禁忌属性言及 redact＋禁止キー構造検査＋PII 再スキャン | ✅ pass（`test_assertive_guard.py` 全件 + integration 非断定契約） |
| TC-11 | S-4: at least one domain `emit_trace_event()` inside each `execute()` | 全実行パスでドメインイベント発火 | ✅ pass（`test_graph_flow.py::test_tc05_audit_events_emitted` — intake_completed / input_parsed / fit_analyzed / potential_assessed / verification_extracted / questions_generated を実ログで確認） |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → AuditLogger | `emit_trace_event()` fires（`agentcore.audit` logger 実キャプチャ） | No silent failures | ✅ pass（`test_pb_runtime_boundaries.py::test_pb1_audit_event_fires`） |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | ✅ pass（`test_state_safety.py` AST scan） |
| PB-3 | L1 → External service | KB/DB/LLM は注入シーム（`shared.services.*` 契約準拠）経由のみで到達・データ取得 | Data retrieved via seam | ✅ pass（`test_pb_runtime_boundaries.py::test_pb3_external_service_via_seam`。実サービス接続は STG 環境で確認） |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | ✅ pass（`test_import_isolation.py` + CI gate） |
| PB-5 | **Checkpoint safety（本テンプレ強化版・docs/02 §5-0）** | invoke→interrupt→resume→完了後の**全 checkpoint** に (a) 直接識別子（氏名等）が無い (b) APPI 要配慮・公正採用禁忌の**全16カテゴリの語彙**が無い（アダプタ prefilter＋トークナイズ経由の本番同等経路で投入） | Inspection pass | ✅ pass（`test_pb_runtime_boundaries.py::test_pb5_checkpoint_no_sensitive_values`） |
| PB-5b | **HITL resume 経路の checkpoint 安全（独立レビュー指摘対応）** | 採用担当の resume 修正指示（日本語氏名＋禁忌語彙＋日本語直後電話番号）をアダプタ境界 `sanitize_resume_feedback` で無害化してから resume。**外側・内側両方の saver の全 checkpoint 断面**に原値が無いことを assert | Inspection pass | ✅ pass（`test_pb_runtime_boundaries.py::test_pb5b_resume_feedback_no_sensitive_values_in_checkpoints`） |
| PB-6 | Invoke execution order | S-1 → node_start → S-2 → execute → S-3 → node_complete（全 FunctionNode 自動発見） | Order verified | ✅ pass（`test_pb_invoke_order.py`。GraphNode 境界と D6 interrupt ノードの扱いは同ファイル内コメントに根拠明記 — interrupt の真の伝播は PB-7 が担保） |
| PB-7 | **HITL interrupt propagation（hitl.enabled: true のため必須）** | 内側 `interrupt()` → GraphNode `propagate_hitl=True` → 外側 caller まで `GraphInterrupt` 伝播。`status` は `awaiting_human`（**error にならない**） | 伝播確認 | ✅ pass（`test_pb_runtime_boundaries.py::test_pb7_hitl_interrupt_propagation`） |

## Business Logic Tests（ドメイン固有 — docs/02 §13 申し送り消化）

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | **非断定契約（schema 構造ガード）** | evaluate 全経路の最終レポート | `recommendation`/`pass_fail`/`hire`/`overall_score`/`composite_score`/`ranking` 系キーが**出力のどこにも存在しない**。軸別 `sufficiency` は high/medium/low/unknown のみ | ✅ pass（`test_graph_flow.py::test_hitl_resume_to_success_produces_non_assertive_report` + `test_assertive_guard.py::test_every_forbidden_key_detected`（全禁止キー網羅 parametrize）） |
| BL-02 | 断定文言の降格（削除でなく降格） | 「採用すべき」「should hire」「合格」等を含む所見 | 「要確認」framing を付加して降格・原文保持（透明性） | ✅ pass（`test_assertive_guard.py`） |
| BL-03 | **母集団閾値縮退** | `population_threshold=20`、cohort n=3 の職種 | `degraded_flags` に `cohort_below_threshold`、evidence に「母集団不足(n=3)」明示、sufficiency は unknown 落ち、**総合点は出さない** | ✅ pass（`test_nodes.py::test_population_below_threshold_degrades_no_score` + `test_graph_flow.py::test_population_below_threshold_degrades_in_report`） |
| BL-04 | **allowlist 射影（入力段階の構造的排除）** | nationality/gender/age/graduation_year 混在 payload | 許可フィールドのみ通過。graduation_year → experience_years 変換（絶対日付破棄）。警告には**内容を記録しない** | ✅ pass（`test_allowlist.py` 全件） |
| BL-05 | **APPI 要配慮カテゴリ別検知（OI-1・必須）** | 全16カテゴリ（人種/信条/社会的身分/病歴/障害/健康診断/診療調剤/犯罪経歴/犯罪被害/本籍出生地/家族/労組/結婚出産/国籍/年齢/性別）各1件 | カテゴリごとに検知・フィールド破棄・`intake_warnings` へカテゴリコードのみ記録 | ✅ pass（`test_appi_dictionary.py::test_each_appi_category_is_detected` — カテゴリ別 parametrize 16件） |
| BL-06 | 全角・大文字小文字バイパス防御 | `ＲＡＣＥ` / `CRIMINAL RECORD` 等 | NFKC 正規化＋casefold 後に照合しヒット | ✅ pass（`test_appi_dictionary.py`） |
| BL-07 | 誤検知（過剰破棄）防御 | `manager` / `storage` / `usage` を含む職務経歴 | ASCII 語彙は単語境界照合 — 部分文字列で誤発火しない | ✅ pass（`test_appi_dictionary.py::test_clean_text_no_false_positive` + server import smoke） |
| BL-08 | **HITL resume→success 到達**（suspend 検証だけでは subgraph キャッシュバグを検出できないため必須） | invoke → approve resume | `awaiting_human` → `success`、レポート＋`case_id` 返却 | ✅ pass（`test_graph_flow.py::test_hitl_resume_to_success_produces_non_assertive_report`） |
| BL-09 | HITL request-revision ループ | correct(修正指示) → approve | 修正指示は question_generation へ条件辺で戻り再提示（盲目的再 interrupt ではない）→ 承認で success | ✅ pass（`test_graph_flow.py::test_hitl_request_revision_loops_then_approves`） |
| BL-10 | 設問→懸念の対応 ID | 懸念 c-01 | 生成設問すべてが `targets_concern` 参照 ID を持つ | ✅ pass（`test_nodes.py::test_questions_carry_concern_reference`） |
| BL-11 | ingest モード（行データ取込） | rows 1件（hire_outcome/performance_score 付き） | 行単位 allowlist 射影→正規化→KB/candidate_cases upsert・取込サマリ返却 | ✅ pass（`test_graph_flow.py::test_ingest_mode`) |
| BL-12 | feedback 部分 UPDATE（冪等） | `case_id`＋`hire_outcome` | 対象列のみ UPDATE・同一入力 2 回で安全（冪等）・`case_id` 不存在は `status="error"` | ✅ pass（`test_graph_flow.py::test_feedback_mode_partial_update` / `test_feedback_unknown_case_id_errors` + `test_nodes.py`） |
| BL-13 | DB INSERT 失敗の縮退 | insert 失敗を注入 | 評価返却をブロックしない（`degraded_flags:["case_insert_failed"]`・`case_id` 空） | ✅ pass（`test_nodes.py::test_case_insert_db_failure_degrades`） |
| BL-14 | PII トークナイズ往復（アダプタ層） | 氏名＋メール入り原文 | tokenize で除去 → グラフ外 resolve で復元（レスポンス整形のみ） | ✅ pass（`test_pii_tokenizer.py` 全件） |
| BL-15 | **resume 補完サニタイザ（detect_pii 既知欠陥対応）** | 「田中様」「080-1234-5678です」「yamada@example.comにも共有」等 | vendored `detect_pii` が取れない日本語氏名（敬称付き）・日本語直後の email/電話を補完正規表現で除去。dict 構造（action/directive）は保持・統計は件数/カテゴリコードのみ・マスク標識は検知語彙を含まない | ✅ pass（`test_identifier_sanitizer.py` 9件） |

## Test Execution Summary
- Execution date: 2026-07-06
- Total tests: **99**（unit 82 / integration 8 / proof_of_boundary 9）
- Pass: 89 / Fail: 0 / Skip: 0
- 実行コマンド: `python -m pytest tests/ -v` および `python -m pytest tests/proof_of_boundary/ -v`（両方 green）
- 備考: LLM/KB/DB は fake 注入（実接続は STG 環境で確認）。credential 形の値はテスト内に存在しない（mock 値のみ）。

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | 外部 CLI 経由のモデルを `BaseLLM` 実装としてラップして注入 |
| 注入シーム | `live_llm_injector` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['reason'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 CLI と鍵が要るため）。

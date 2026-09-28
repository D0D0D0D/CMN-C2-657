# Template Design Specification — CMN-C2-657 CandidateFitAndPerformancePotentialReportAgent

> Proposal reference: `docs/01_proposal.md`.
> 本書は設計仕様。実装はこの設計に従う。

---

## 1. Position in AgentCore Architecture

- **Agent Class**: `CandidateFitReportGraph`
- **L1 Base**: `AgentBaseGraph`（L1 直継承。`AutonomousBaseGraph` ではない）
- **Composition Pattern**: Cat 2 標準 — 固定5ノード backbone の `main` スロットに `GraphNode` サブクラス（`CandidateAssessmentGraphNode`）を置き、内側に `CandidateAssessmentWorkflow`（`BaseGraph` 直継承・カスタムトポロジ）を持つ
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 禁止 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` の override のみ）
  - Graph: composition（`register_nodes()` によるノード差し込み。外側 `add_edges()` は override しない）

### 1-1. 全体構成図

```
[HTTP] /ingest ─┐
[HTTP] /invoke ─┼─ src/api/server.py（APIアダプタ層）
[HTTP] /feedback┘   │ ①モード付与（mode=ingest|evaluate|feedback）
                    │ ②PII トークナイズ（直接識別子の除去・参照ID化）★グラフ外
                    │ ③invoke 戻り値の整形（S-3 通過済み出力のみ返却）
                    ▼
    CandidateFitReportGraph（外側・AgentBaseGraph 継承）
    START → initialize → pre_process → main → {route} → post_process → finalize → END
                          │             │                  │
                          │             │                  └ ReportComposeNode（FunctionNode）
                          │             └ CandidateAssessmentGraphNode（GraphNode）
                          └ CandidateIntakeNode（FunctionNode）
                                        │
                                        ▼ get_subgraph()（インスタンスキャッシュ必須）
    CandidateAssessmentWorkflow（内側・BaseGraph 継承・src/graph/domain_workflow_graph.py）
    START → input_parse → {mode_route}
                        ─┬→ ingest_normalize → kb_upsert ──────────────────────→ END
                         ├→ fit_analysis → potential_assessment → verification_points
                         │       → question_generation → hitl_review(interrupt) → case_insert → END
                         └→ feedback_update ───────────────────────────────────→ END
```

---

## 2. Architecture Overview

### 2-1. Node Configuration（外側グラフ）

| Node | クラス | 責務 | Input State | Output State（変更キーのみ） | Inherits |
|------|--------|------|-------------|------------------------------|----------|
| initialize | (framework) | セッション初期化 | — | — | InitializeNode (default) |
| pre_process | `CandidateIntakeNode` | mode 検証（`ingest`/`evaluate`/`feedback`）、入力の構造化（候補者データ→allowlist 射影、job_category 正規化）、S-2 追加ゲート（§5）、大容量書類の段階要約 | `user_input`, `input_context` | `validated_input`, `mode`, `job_category`, `phase`, `intake_warnings`, `status` | FunctionNode |
| main | `CandidateAssessmentGraphNode` | 内側 subgraph の起動・入出力マッピング。上流 `status=="error"` はパススルー（実行しない） | `validated_input`, `mode` | `assessment_payload`, `case_id`, `degraded_flags`, `status` | GraphNode |
| post_process | `ReportComposeNode` | 評価レポート（構造化JSON＋Markdown）の統合生成、S-3 追加ゲート（断定文言検査・禁忌属性言及検査・PII サニタイズ、§5） | `assessment_payload`, `case_id` | `result`, `status` | FunctionNode |
| finalize | (framework) | 出力射影（`result`→`output`） | — | — | FinalizeNode (default) |

- 外側 `add_edges()` / `route()` は **override しない**（framework 配線に従う）。
- 各ノードは**変更したキーのみ**返す（full state 返却禁止 — `node_history`/`error_log` は reducer のため二重化する）。
- `_invoke_impl()` はノードに書かない（`AgentBaseGraph` のみ保持）。

### 2-2. Node Configuration（内側グラフ `CandidateAssessmentWorkflow`）

内側は `BaseGraph` 直継承（3モードの分岐トポロジのため）。`register_nodes()` は super() を呼ばず、initialize/finalize は登録しない（外側 backbone の責務）。

| 内側 Node | クラス | 責務 |
|---|---|---|
| input_parse | `InputParseNode` | **内側グラフのエントリノード**。外側 `extract_input()` が渡す JSON 文字列（`user_input`）を `json.loads` し、`mode` / `job_category` / `phase` / `case_id` / 構造化候補者データを `InnerState` の各欄へ復元する（内側は `user_input(str)`＋`ctx` しか受領しないため、この復元ノードなしに `mode_route` は mode を参照できない）。パース失敗は `status="error"` |
| mode_route | （分岐関数。ノードではなく `add_conditional_edges` のルート関数。input_parse の直後に配線） | `state["mode"]` により ingest / evaluate / feedback へ分岐。**分岐関数の state 注釈は自 schema（`InnerState`）にする**（`AgentState` 注釈だとカスタム欄が射影で消え誤 route する — 実機確定事項） |
| ingest_normalize | `IngestNormalizeNode` | CSV/Excel 由来の行データを LLM 列マッピングで正規化。**この段階で allowlist 射影を適用し、禁忌属性列・直接識別子列は破棄** |
| kb_upsert | `KbUpsertNode` | 正規化済みデータを採用ナレッジKB（RAG）と `candidate_cases` へ upsert。取込サマリ（件数・破棄列・警告）を返す |
| fit_analysis | `FitAnalysisNode` | 候補者（allowlist 射影済み）× 自社基準（RAG 検索）の接点・乖離を**根拠引用付き**で特定 |
| potential_assessment | `PotentialAssessmentNode` | 蓄積した活躍者特徴（`candidate_cases` の職種別分布＋RAG）に照らし、評価軸ごとの充足度・懸念を**根拠引用付き**で評価。母集団サイズ閾値未満は縮退（`degraded_flags` に明示）。**総合点・順位は算出しない** |
| verification_points | `VerificationPointsNode` | まだ確かめられていない懸念・要確認スキルを抽出し `concern_flags`（種別コード＋根拠ポインタ）へ |
| question_generation | `QuestionGenerationNode` | 各懸念に対応する構造化面接設問＋評価ルーブリックを生成（設問→懸念の対応 ID を必ず持つ） |
| hitl_review | `HitlReviewNode` | **D6 `interrupt()`**。レポートドラフトを人間（採用担当・面接官）へ提示し、承認／修正指示を受領（§6）。修正指示なら question_generation / potential_assessment へ戻る条件辺 |
| case_insert | `CaseInsertNode` | 最小化・匿名化済みレコードを `candidate_cases` へ INSERT（`hire_outcome=pending`）、`case_id` 採番。書込失敗は評価返却をブロックしない（warning 化） |
| feedback_update | `FeedbackUpdateNode` | `case_id` 必須。`hire_outcome`（即時）／`performance_score`・`evaluator_edited`（遅延）の**対象列のみ部分 UPDATE**（冪等）。存在しない `case_id` はエラー |

**GraphNode 契約の実装指針（実機確定事項の反映）:**

- `get_subgraph()`: 内側グラフを `self._subgraph` に**キャッシュして返す**（毎回新規生成は resume 時に checkpointer を失い interrupt 無限ループになる）。
- `extract_input()`: `validated_input` ＋ `mode`/`job_category`/`case_id` 等を **JSON 直列化（`default=str`）した文字列**で渡す（内側は `user_input(str)`＋`ctx` のみ受領・外側 `input_context` は非伝播のため）。内側エントリの `InputParseNode` がこれを `InnerState` へ復元する（§2-2 と対で設計）。
- `merge_output()`: 内側 `get_output()` の dict から**変更キーのみ**返す。HITL 使用のため `hitl_draft` / `hitl_feedback` / `hitl_status` を merge 対象に含める（実機確定事項）。
- `error_strategy = "handle"`: 内側の DB 書込失敗等は `on_subgraph_error()` で縮退（評価は返す・蓄積のみ失敗を warning）。LLM 呼び出し失敗など評価そのものの失敗は `status="error"` で返す。
- `propagate_hitl = True`: 内側の interrupt を外側呼び出し元へ伝播（外側 config にも `hitl.enabled: true` が必要）。

### 2-3. Data Flow（外側）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (RETRY, max 3)
                                  pre_process
```

`route()` は framework 実装（`state["status"]` を読む）。override しない。

### 2-4. State Definition

**外側 State（`src/schemas/state.py` — `AgentState` 継承 flat TypedDict）**

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `"ingest"` / `"evaluate"` / `"feedback"` | ✅ |
| `validated_input` | `str` | allowlist 射影・PIIトークナイズ済みの構造化入力（JSON文字列） | ✅ |
| `job_category` | `str` | 正規化済み職種（照合の母集団キー） | evaluate 時 |
| `phase` | `str` | `"document"` / `"pre_interview"` / `"post_interview"` | evaluate 時 |
| `case_id` | `str` | 評価ケースID（evaluate で採番／feedback で必須入力） | feedback 時 |
| `assessment_payload` | `dict` | 内側グラフの評価結果（引用・懸念・設問。すべて非機密化済み） | — |
| `degraded_flags` | `list[str]` | 縮退情報（KB空・母集団不足・DB書込失敗 等） | — |
| `intake_warnings` | `list[str]` | 入力段階の警告（破棄した列・要約実施 等） | — |
| `result` | `dict` | 最終レポート（S-3 通過済み） | — |
| `hitl_draft` / `hitl_feedback` / `hitl_status` 等 | （framework 定義7欄） | HITL（schema 1.4 の7欄のみ。`hitl_count` はテンプレ書込禁止） | — |

**State Constraints（必須）:**
- flat TypedDict のみ（primitives + JSON-serializable）
- JWT / API key / credential を State に置かない（checkpoint DB へ平文残留するため）
- InvocationContext は `config["configurable"]` 経由のみ（State 非格納）
- Pydantic / dataclass / 任意 Python オブジェクト禁止
- **候補者の直接識別子（氏名・連絡先・生年月日等）の実値を State に置かない**（§5-0 参照ID方式。checkpointer は super-step ごとに State を永続化するため、グラフ内のどのノード戻り値にも実値を載せない）

**内側 State（`InnerState` — 独自 TypedDict）**: `user_input` / `mode` / `job_category` / `fit_findings` / `axis_assessments` / `concern_flags` / `interview_questions` / `case_id` / `status` 等。分岐関数・`get_output()` の注釈は必ず `InnerState`。

### 2-5. 出力 schema（候補者評価レポート）

```jsonc
{
  "case_id": "cc-2026-000123",
  "meta": { "job_category": "...", "phase": "post_interview",
            "generated_at": "...", "kb_coverage": "full|degraded",
            "authorship": "ai_draft" },            // AI起草であることの明示
  "fit_analysis": [                                  // ①適合性
    { "axis": "axis_code", "alignment": "match|gap|unknown",
      "evidence": [ { "source": "interview_memo", "quote": "…該当記述の引用…" } ],
      "note": "…" } ],
  "potential_assessment": [                          // ②活躍可能性（軸別・総合点なし）
    { "axis": "axis_code", "sufficiency": "high|medium|low|unknown",
      "concerns": ["concern_id"],
      "evidence": [ { "source": "resume", "quote": "…" },
                    { "source": "candidate_cases", "stat": "職種内 n=42 の匿名集計" } ] } ],
  "verification_points": [                           // ③検証論点
    { "concern_id": "c-01", "description": "…", "why_unverified": "…" } ],
  "interview_questions": [                           // ④検証設問（懸念に必ず紐づく）
    { "question_id": "q-01", "targets_concern": "c-01", "question": "…",
      "rubric": { "good": "…", "acceptable": "…", "insufficient": "…" } } ],
  "similar_cases": [ { "summary": "匿名集計のみ（個票は返さない）" } ],
  "human_review": { "status": "confirmed|edited",    // HITL 通過後のみ確定
                    "edited_sections": ["…"] },
  "disclaimers": ["本レポートは意思決定支援のドラフトであり、採用可否の判断・確定は人間が行う", "…"]
}
```

**構造的ガード（非断定契約の物理設計）:**
- **合否・採用推奨フィールドは schema に存在しない**（`recommendation` / `pass_fail` / `hire` 系キーなし）。
- **総合スコア・候補者間ランキングを算出しない**（軸別 `sufficiency` のみ。単一数値がないため閾値による自動足切りに転用できない）。
- すべての所見（`fit_analysis` / `potential_assessment`）は `evidence`（入力のどの記述か＝引用、または `candidate_cases` の匿名統計）を必須で持つ。evidence を構成できない所見は `sufficiency:"unknown"` に落とす（透明性要件）。
- `authorship:"ai_draft"` と `human_review.status` により「AI の所見」と「人間の判断」がレポート上で常に区別される。

---

## 3. モード設計と API アダプタ（server.py）

| ルート | mode | 動作 | 書込 |
|---|---|---|---|
| `POST /ingest` | `ingest` | CSV/Excel 由来行データの列マッピング・正規化 → KB/`candidate_cases` へ upsert | upsert |
| `POST /invoke` | `evaluate` | 適合性分析→活躍可能性評価→検証論点→検証設問→HITL→レポート返却 | 暫定 INSERT（`hire_outcome=pending`） |
| `POST /feedback` | `feedback` | `case_id`＋結果の受領 | 対象列のみ部分 UPDATE（冪等） |

- 3ルートとも**同一グラフ**を `mode` 付きで invoke し、single-entry 設計との整合を維持する。
- **PII トークナイズはアダプタ層（グラフ外）で実施**: 直接識別子（氏名・連絡先・生年月日・住所・顔写真参照）を `shared.security.pii_detector`＋決定的トークナイザで除去/置換（氏名→`[CANDIDATE]`）。マッピングはリクエストスコープのメモリのみに保持し**永続化しない**。グラフ・checkpoint には実値が入らない（実機確定事項: グラフ内 resolve は全て checkpoint 漏洩）。レポートは `[CANDIDATE]` 表記のまま返し、表示名の復元が要る場合もアダプタ層の戻り値整形でのみ行う。
- HITL resume（`POST /invoke` に `session_id`＋`resume` ペイロード）も server.py が受け、同一 `thread_id` で `Command(resume=...)` を渡す。
- 外側・内側とも**明示 `compile(checkpointer=)`** が必要（config からの自動生成はされない — 実機確定事項）。AgentRegistry は `config/config.yaml` の `hitl` / `memory_enabled` を Graph(config=) に渡し、standalone の server.py は同じ値を明示的な runtime config として渡す。

---

## 4. コンプライアンス設計（Researcher 所見 [RES-JUL04-F02] への設計上の回答）

> 本セクションが本テンプレートの生命線。設計レビュー・顧客説明の一次参照とする。

### 4-1. Item 1 — EU AI Act Annex III §1 アセスメント

**明示的 in/out 判定: IN SCOPE（Annex III §1 該当）と判定する。**

- **判定根拠**: 本 AG は「採用（recruitment/selection）における自然人の評価」に用いられる AI システムであり、Annex III §1(a) の文言に直接該当する。「採用可否を断定しない」ことは該当性を消さない — Researcher 指摘のとおり、構造化された軸別評価は採用担当の判断に**実質的影響（materially influences）**を与えるため、Art.6 の High-Risk 例外（narrow procedural task 等）には依拠できない／しない。したがって **out-of-scope を主張せず、High-Risk 該当を前提に設計義務を満たす**のが本設計の立場である。
- **AI と人間の責任分界（判定の帰属）**: AI が行うのは「根拠付き所見ドラフトの生成」まで。採否判断・レポート確定は人間（採用担当・面接官）が行い、その**証跡は S-4 イベントで残る**（`hitl_presented` → `hitl_resolved`（承認/編集の別・編集セクション）→ `feedback_updated`（人間が確定した採否の記録））。「どこまでが AI・どこからが人間か」は監査ログ上で常に再構成できる。
- **市場前提**: 一次ターゲットは国内企業の採用部門。EU 域内での上市・EU 所在候補者への使用時は、提供者（provider）/導入者（deployer）としての適合性評価・EU データベース登録（Art.49）等の追加義務が発生する。この**責任分界（プラットフォーム提供者／テンプレート導入顧客のどちらが provider/deployer に当たるかの整理を含む）は導入前提条件**として `docs/07_operation_guide.md` に明記する（設計側の対応は下表・手続側の履行は導入時）。
- **リスクマネジメント（Art.9 対応）**: 断定→バイアス／PII 漏洩／誤評価過信／禁止質問誤検出／コールドスタートを初期リスクセットとして、①危険シナリオ識別 → ②設計緩和（本書 §2-5/§4/§5/§6） → ③残余リスク監視（`evaluator_edited` 率・評価と `hire_outcome`/`performance_score` の乖離率・S-3 降格発生率をメトリクス化し docs/09_process_metrics.md へ集約）のサイクルを運用に組み込む。閾値逸脱時は KB/プロンプトを是正する。
- **技術文書（Art.11 対応）**: 本リポジトリの設計書（本書）とテスト仕様を Annex IV 技術文書の基礎ドキュメントとして維持する。
- **High-Risk 該当を前提とした設計要件**:

| EU AI Act の要求（High-Risk 主要義務） | 本設計での対応 |
|---|---|
| Human oversight（Art.14） | HITL 必須（§6）。レポートは人間の承認・編集を経てのみ `human_review.status=confirmed` になる。自動確定経路は存在しない |
| 自動意思決定の排除 | 出力 schema に合否・推奨・総合スコアが**構造的に存在しない**（§2-5）。閾値足切りに転用可能な単一数値を返さない |
| Transparency（Art.13） | 全所見に `evidence`（入力引用 or 匿名統計）必須。`authorship:"ai_draft"` 明示。`disclaimers` 固定文 |
| Logging / traceability（Art.12） | S-4 監査ログ（§5-4）: 評価実行・HITL 承認/編集・蓄積・feedback UPDATE を `emit_trace_event()` で全記録 |
| Data governance（Art.10） | allowlist 射影（§4-3）で禁忌属性・代理変数を学習/照合データから排除。`/ingest` 時も同一射影を適用 |
| Accuracy 監視（Art.15 相当） | `evaluator_edited` 率・評価と実結果（`hire_outcome`/`performance_score`）の乖離率を運用メトリクスとして保持 |

- **根拠（構造化スコアの実質的影響への回答）**: Researcher 指摘のとおり「断定しない」だけでは不十分。本設計は (a) 判断に転用しやすい**集約値そのものを出さない**、(b) 軸別評価も evidence なしには出せない、(c) 人間レビューを通らない出力は「確定」ステータスに到達しない、の3点で「AI が実質的に採否を決める」構造を物理的に塞ぐ。

### 4-2. Item 2 — APPI 2026 §17 要配慮個人情報・保持/破棄の形式化

**何をスキャンするか（S-2）:**
- アダプタ層: `shared.security.pii_detector` で直接識別子（氏名・連絡先・生年月日・住所）を検出しトークナイズ（§3）。
- pre_process（`_extra_security_gate_input()`）: (a) allowlist 外フィールドの混入検査（許可フィールド: `job_category` / `phase` / `experience_years` / `skill_tags` / 職務経験記述 / 面接応答記述 / 評価軸コード）、(b) **要配慮個人情報・公正採用禁忌属性の語彙検査** — 検査対象カテゴリは **APPI §2 の要配慮個人情報を全カテゴリ網羅**する: 人種・信条（思想/宗教）・社会的身分・**病歴・心身の障害・健康診断結果・診療/調剤情報**・**犯罪の経歴・犯罪被害事実**、加えて公正採用選考上の禁忌属性（本籍/出生地・家族構成/家庭環境・労働組合加盟・結婚出産予定・国籍・年齢・性別）。該当フィールドは**評価パイプラインに載せず破棄**し、`intake_warnings` に破棄した旨のみ記録（＝内容は記録しない）。検知辞書はカテゴリ別語彙辞書（`src/services/` に静的定義・テスト対象）で管理し、カテゴリごとの検知/破棄テストを TC に含める、(c) 入力サイズ上限。

**何を保持し、何を破棄するか:**

| データ | 扱い |
|---|---|
| 候補者原文（履歴書・面接メモ） | **保持しない**。リクエスト処理中のみメモリ保持し、evaluate 完了で破棄。DB・KB・checkpoint に入れない |
| 直接識別子 | アダプタ層で除去。グラフ・checkpoint・DB に入らない |
| 評価レポート本文（自由記述） | **DB に恒常保管しない**（`/invoke` 応答としてのみ返却）。データ最小化要件を形式化 |
| `candidate_cases` レコード | 最小化・匿名化済みのコード/数値のみ（`job_category`・`experience_years`・`skill_tags`（正規化タグ）・`fit_scores`（軸コード×充足度コード）・`concern_flags`（種別コード）・`hire_outcome`・`performance_score`・`evaluator_edited`）。個票から個人を特定できるフィールドを持たない |
| checkpoint（HITL 用） | トークナイズ済み・allowlist 射影済みデータのみ。TTL を設け session 完了後に削除（`checkpoint_ttl_hours`） |

**匿名化の実装**: 「LLM で頑張って匿名化する」のではなく、**allowlist 射影＝許可フィールド以外を通さない構造**で実現する（決定的・検証可能）。`skill_tags` は正規化済みタグ辞書との突合で自由記述を排除。

**削除・保持（データ主体対応）:**
- `case_id` 単位の削除 API（運用手順は docs/07）— APPI 本人削除・利用停止請求への対応経路。`performance_score` 未確定の長期滞留レコードも `case_id` で特定・削除可能。
- 保持期間上限: `retention_months`（config・既定 60）。超過レコードは定期パージ。
- 要配慮個人情報は**そもそも収集・格納しない設計**（allowlist に該当フィールドが存在しない）。

### 4-3. Item 3 — 労働施策総合推進法・代理変数差別の除外

**スコアリングロジックからの法的禁止基準の除外（文書化）:**
1. **入力段階（構造的除外）**: 評価に使うのは allowlist 許可フィールドのみ。年齢・性別・国籍およびその直接表現は allowlist に存在しないため、評価 LLM のプロンプトに**物理的に到達しない**。生の候補者原文は活躍可能性評価の判断材料に直接使わない（allowlist 射影済み構造化データのみ渡す）。
2. **代理変数への対処**: 卒業年・生年から年齢が復元できるため、日付系は `experience_years`（経験年数・数値）へ変換して以外は破棄。氏名（国籍・性別の代理）はアダプタ層でトークナイズ済み。母国語・出身地に類する記述は allowlist 外として破棄。
3. **`/ingest` 時（過去バイアスの符号化防止）**: 取込データにも同一の allowlist 射影を適用。過去の面接評価文に禁忌属性への言及が含まれる場合、該当フィールドは KB へ入れず破棄。**「活躍者特徴」の照合キーは `job_category` のみ**（属性別の照合キーを持たない＝属性別の異なる扱いが構造的に不可能）。
4. **出力段階（S-3）**: `_extra_security_gate_output()` でレポート・面接設問に禁忌属性への言及（本籍・家族・思想・結婚出産・国籍・年齢・性別を評価根拠とする記述）がないか語彙検査。検出時は該当所見を除去し `degraded_flags` に記録。
5. **禁止質問の自動判定はしない**（誤検出リスクを避けるため射程外）。生成設問は HITL で面接官が最終確認する前提を `disclaimers` とガイドに明示。
6. **モニタリングの限界の明示**: 保護属性を保持しないため属性別の統計監視は**できない**。これは「事後監視より発生源除外」を選んだ意図的トレードオフであり、その旨を本書に記録する。

---

## 5. セキュリティ設計（5層）

### 5-0. 参照ID方式（State/checkpoint の不変条件 — 何を許し何を禁じるか）

State/checkpoint に関する不変条件を、レベル分けして正確に定義する（「センシティブ値ゼロ」の粒度を曖昧にしない）:

| データ種別 | State/checkpoint への格納 | 根拠・扱い |
|---|---|---|
| 直接識別子（氏名・連絡先・生年月日・住所・顔写真参照） | **禁止（絶対）** | アダプタ層でトークナイズ済み（§3）。グラフ内のどのノード戻り値にも載らない |
| 要配慮個人情報・禁忌属性（§4-2 の全カテゴリ） | **禁止（絶対）** | S-2 allowlist 射影で入力段階破棄。State に到達しない |
| credential / 接続情報 | **禁止（絶対）** | `ctx.secrets` のみ |
| トークナイズ・allowlist 射影済みの職務関連テキスト（`validated_input`、evidence の `quote`） | **セッション中のみ許容** | 評価 LLM の作業素材として State を経由することは避けられない（これなしに評価は成立しない）。ただし①直接識別子は除去済み、②`quote` は State 格納前に pii_detector を再適用、③checkpoint は `checkpoint_ttl_hours`（120h・HITL 承認が週跨ぎになるケースを考慮した初期値。テンプレ利用者が自環境の承認リードタイムに合わせ調整する前提）で削除、④DB/KB へは恒常保存しない（`candidate_cases` はコード/数値のみ・§4-2） |
| コード/数値フィールド（`case_id`・軸コード・充足度コード等） | 許容 | 非機密 |

- したがって §2-4 の `validated_input` / `assessment_payload` の State 保持は本不変条件と整合する（**恒常保管の禁止**であり、セッションスコープの通過を禁じるものではない。恒常化を防ぐ機構が TTL 削除と DB 最小化）。
- PB テストで invoke→interrupt→resume→完了後の**全 checkpoint に直接識別子・禁忌属性語彙が無い**ことを assert する（トークナイズ済みテキストの存在は許容・TTL 削除は運用テストで確認）。

### 5-1. S-1 Trust Gate

- **全 FunctionNode / GraphNode に `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を明示宣言**（クラス本体。暗黙 ANONYMOUS 継承は不可）。根拠: 候補者個人データの処理＋DB/RAG 書込を伴う特権操作。
- 有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` のみ。`BaseNode.__call__()` の S-1 ゲートは override しない。

### 5-2. S-2 Input Security Gate

- `FunctionNode` の `@final _security_gate_input()`（PII スキャン）は自動実行。テンプレは `_extra_security_gate_input()` フックのみ実装:
  - `CandidateIntakeNode`: allowlist 外フィールド検査／禁忌属性フィールドの破棄（§4-2）／入力サイズ上限／mode 妥当性（未知 mode は `SecurityViolationError` ではなく `status="error"`＋理由）。
  - 内側 `IngestNormalizeNode`・`FeedbackUpdateNode` にも同フックで allowlist 射影・`case_id` 形式検査。
- `CandidateAssessmentGraphNode`（GraphNode）: S-2/S-3 は意図的 no-op パススルー（上流 pre_process と内側各ノードでゲート済み — ADR-017 の設計選択であり bypass ではない）。

### 5-3. S-3 Output Security Gate

- `@final _security_gate_output()`（credential スキャン）は自動実行。`ReportComposeNode._extra_security_gate_output()` で:
  1. **断定文言検査**: 「採用すべき」「不採用」「合格/不合格」等の確定語彙を検出したら該当所見を「要確認」へ**降格**（削除でなく降格＝透明性維持）。
  2. **禁忌属性言及検査**（§4-3-4）。
  3. **PII 再検査**: 出力 JSON の全文字列値に対し pii_detector を再適用（入力引用 `quote` に混入した識別子の除去）。
- グラフ内で参照IDの resolve はしない（S-3 は安全検査のみ）。

### 5-4. S-4 Audit Logging

- 各ノードの `execute()` 内で `emit_trace_event(event_type, payload, state)` を**最低1回**（`shared.utils.audit_logger`・3引数）。`node_start`/`node_complete`/`node_error` は framework が自動発行するため**重複させない**。
- ドメインイベント設計: `intake_completed` / `ingest_upserted` / `fit_analyzed` / `potential_assessed` / `verification_extracted` / `questions_generated` / `hitl_presented` / `hitl_resolved`（承認 or 編集の別・編集セクション） / `case_inserted` / `feedback_updated` / `report_composed`。payload に PII を含めない（コード・件数・`case_id` のみ）。

### 5-5. S-5 Credential / Secrets

- secrets は `ctx.secrets.require("KEY")` のみ（`os.environ` 禁止・State 非格納）。テストは注入シーム（fake SecretProvider / `current_secrets().get()`）。
- `pyproject.toml` は全依存 `==` 完全 pin（`requires-python` のみ範囲可）。

---

## 6. HITL 設計（D6 interrupt・人間確定フロー）

- **位置**: 内側 `HitlReviewNode`（question_generation の後）。レポートドラフト＋懸念・設問一覧を `interrupt({"draft": ..., "reason": "human review required"})` で提示。
- **必須運用**: `human_review.status` は HITL を通らない限り `confirmed` にならない（＝自動確定なし。EU AI Act Art.14 対応の実装点）。
- **応答の分岐**:
  - 承認 → `case_insert` へ（`evaluator_edited=false`）。
  - 編集（所見・設問の修正テキスト） → 編集を反映して `case_insert` へ（`evaluator_edited=true`・改善シグナルとして蓄積）。
  - **修正指示（壁打ち）** → 「この懸念の検証設問を技術面に寄せて」等の指示なら question_generation（設問のみ）または potential_assessment（評価やり直し）へ戻り、再度 `hitl_review` に至る。**この対話 refine は正常系**であり回数上限で縛る UX にしない（`max_hitl` は framework 要求の安全弁として 8 を設定するに留める）。新規の候補者データ取り直し・KB 再検索は明示要求時のみ（Cat 2 境界）。
- **実装制約（実機確定事項の反映）**:
  - `hitl_draft` に生成済みドラフトを保存し resume 時の LLM 再実行を防ぐ（冪等ガード）。
  - `hitl_count` はテンプレから書き込まない（backbone 専用）。max_hitl ブレーカーは Cat 3 専用であり Cat 2 では参照されない。
  - `memory_enabled: true` 必須。内側・外側とも**明示 `compile(checkpointer=)`**。`get_subgraph()` はキャッシュ（§2-2）。
  - `propagate_hitl = True`（外側呼び出し元＝server.py まで interrupt を戻す）。
- PB-7（interrupt が `GraphInterrupt` として engine に到達し `status` が `error` にならない）をテスト対象に含める。

---

## 7. Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, caller_trust_level, hitl_allowed。`config["configurable"]` 経由）
- [x] SecurityViolationError（S-2 違反時）
- [x] S-2: `_extra_security_gate_input()` — §5-2（`@final` の `_security_gate_input()` は override しない）
- [x] S-3: `_extra_security_gate_output()` — §5-3（同上）
- [x] S-4: `emit_trace_event()` — §5-4（各 execute() 内最低1回。node_start/complete は発行しない）
- [x] `shared.services.llm.BaseLLM`（コンストラクタ注入。テストは fake 注入）
- [x] `shared.security.pii_detector`
- [x] `shared.services.database`（`candidate_cases`。接続は `ctx.secrets` 経由）
- [x] 採用ナレッジKB（ベクトルストア。`src/services/kb_service.py` に接続シームを実装）

### Composition Pattern
- **Pattern**: GraphNode (subgraph)
- **Composition target**: `CandidateAssessmentWorkflow`（`src/graph/domain_workflow_graph.py`）
- **Error propagation strategy**: `handle`（蓄積系の失敗は縮退＝評価は必ず返す。評価系の失敗は `status="error"`。合成境界の要件として、戦略と縮退挙動を本書で定義済み）

## 8. Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only（`mediator/`・他テンプレート import なし）

---

## 9. config/agent.yaml 設計値

```yaml
agent:
  id: "CMN-C2-657"
  name: "CandidateFitAndPerformancePotentialReportAgent"
  version: "0.1.0"
  category: "Cat 2"
  industry: "CMN"
  base_type: "AgentBaseGraph"          # L1 base class (conceptual pattern: assessment+docgen)
  module: "src.graph"
  class: "CandidateFitReportGraph"
  config:
    max_retry: 3
    timeout_seconds: 120               # LLM 多段評価＋RAG 照合のため 30 から引き上げ
    memory_enabled: true               # HITL 必須（checkpointer）
    hitl:
      enabled: true
      max_hitl: 8                      # framework 安全弁（UX 上限ではない・§6）
    population_threshold: 20           # 活躍者特徴照合の母集団下限（未満は縮退）
    retention_months: 60               # candidate_cases 保持上限（§4-2）
    checkpoint_ttl_hours: 120          # HITL セッション checkpoint の保持上限（週跨ぎ承認を考慮した初期値。導入環境の承認リードタイムに合わせ調整する前提）
    summarize_threshold_chars: 20000   # 大容量書類の段階要約発動閾値
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    secrets:
      - LLM_API_KEY                    # 評価・正規化用 LLM
      - CANDIDATE_DB_DSN               # candidate_cases（顧客テナント内 DB）
      - RECRUIT_KB_URL                 # 採用ナレッジKB（ベクトルストア）
      - RECRUIT_KB_API_KEY
```

---

## 10. エラー・フォールバック設計（ユーザーへ何を返すか）

| 障害 | 挙動 | ユーザーへの返却 |
|---|---|---|
| 採用ナレッジKB が空/未達 | 適合性分析は入力基準のみで実施・活躍可能性評価は縮退 | レポートは返す。`meta.kb_coverage="degraded"`＋「自社基準 KB 未整備のため一般観点で評価」の明示 |
| `candidate_cases` 母集団 < 閾値 | 活躍者特徴照合を縮退（統計裏付け弱い旨を明示） | 軸別評価は返す。該当 evidence に `stat:"母集団不足(n=…)"` |
| DB INSERT 失敗（evaluate） | 評価返却をブロックしない | レポート＋`degraded_flags:["case_insert_failed"]`（`case_id` なし・再実行案内） |
| `/feedback` の `case_id` 不存在 | 部分 UPDATE せずエラー | `status="error"`＋「case_id を確認」 |
| LLM 失敗 | リトライ（backbone RETRY 最大3）後 `status="error"` | 失敗理由＋再実行案内（部分結果は返さない＝誤読防止） |
| 入力が禁忌属性のみ/評価素材不足 | 評価せず intake で停止 | 「評価に使用できる職務関連情報が不足」＋必要な入力例 |

---

## 11. ユーザビリティ設計レビュー（設計判断記録）

**想定ユースケース**: 面接官が面接直後の 5〜10 分で、面接メモ（自由テキスト）と職種を投げて所見ドラフトを得る。評価会議にはレポートをそのまま持ち込む。

本設計を書く過程で、ユーザビリティ観点から以下をレビューし**修正した点**:

1. **入力の最小化（修正）**: 当初案は「候補者データ＋評価基準＋フェーズ＋職種」の構造化入力を要求していたが、**必須入力を「候補者データ（自由テキスト/ファイル）＋ job_category」の2点に削減**。`phase` は入力内容から推定（面接記録があれば post_interview）し、明示指定があれば従う。評価基準は KB から自動取得（都度添付不要）。過剰な構造化入力の要求を排除した。
2. **出力を会議資料形式に（修正）**: 構造化 JSON のみでなく、**評価会議にそのまま使える Markdown レンダリング（軸別所見→懸念→次フェーズ設問の順）を `result` に併載**。設問は「そのまま読み上げられる質問文＋ルーブリック」形式とした。
3. **AI 所見と人間判断の区別（修正）**: `authorship:"ai_draft"` と `human_review.status`/`edited_sections` をレポート必須欄に昇格。「誰の判断か」がレポート単体で常に判別できる（当初はメタ情報扱いだった）。
4. **壁打ち refine を正常系に（修正）**: HITL を「承認/却下」の1回ゲートから、**修正指示→部分再生成→再提示の対話ループ**へ拡張（§6）。回数上限は UX 制約にしない。
5. **縮退の可視化**: KB 未整備・母集団不足でも「使えない」ではなく、何が縮退しているかを明示して部分価値（適合性分析＋設問生成）を返す（§10）。導入初日から使える。
6. **`/feedback` の入力障壁低減**: 採用可否だけの最小ペイロード（`case_id`＋`hire_outcome`）を許容し、活躍度は数年後に別送可能（部分 UPDATE・冪等）。「全部揃わないと送れない」を排除。

## 12. Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **A** | 固定パイプライン（構造化→評価→設問→蓄積）であり自律ループ不要。Cat 2 標準 |
| Composition pattern | Standalone（main=FunctionNode） | GraphNode + 内側 subgraph | **B** | 3モード分岐＋多段評価＋HITL を単一 FunctionNode に詰めると責務過多。Cat 2 標準構成 |
| 内側グラフ基底 | AgentBaseGraph（5ノード） | BaseGraph（カスタムトポロジ） | **B** | mode 3分岐＋HITL 戻り辺のカスタムトポロジが必要 |
| PII トークナイズ位置 | グラフ内 pre_process | API アダプタ層（グラフ外） | **B** | checkpoint 永続化により、グラフ内に一度でも実値が載れば漏洩（実機確定事項）。resolve/tokenize はアダプタ層のみ |
| 禁忌属性の扱い | LLM で検出・除去 | allowlist 射影（許可フィールドのみ通す） | **B** | 決定的・検証可能・誤検出なし。本設計で確定済みの方針 |
| 合否の抑止 | プロンプト制約のみ | 出力 schema から合否/総合点フィールドを構造的に排除＋S-3 降格検査 | **B** | プロンプトだけでは EU AI Act Annex III の「実質的影響」懸念に答えられない。物理設計で塞ぐ |
| HITL | なし（レポート返却のみ） | D6 interrupt による人間確定必須 | **B** | コンプライアンス条件（自動確定なし・Art.14 human oversight）＋evaluator_edited 蓄積が moat 構成要素 |
| エラー伝播 | propagate | handle（蓄積失敗は縮退） | **handle** | 「蓄積はベストエフォート・評価は必ず返す」 |

---

## 13. Open Items（実装フェーズ以降への申し送り・レビュー確定事項）

設計レビュー（2026-07-06）で承認された設計に付帯する TODO。実装・テスト・運用ガイドの各ステージで必ず消化する:

| # | 申し送り先 | 内容 | 由来 |
|---|---|---|---|
| OI-1 | `docs/03_test_spec.md`（実装フェーズ） | **APPI 要配慮個人情報のカテゴリ別検知テストを必須 TC とする**（§4-2 のカテゴリ別語彙辞書に対し、人種/信条/社会的身分/病歴/障害/健康診断/診療調剤/犯罪経歴/犯罪被害＋公正採用禁忌属性の各カテゴリで検知・破棄・`intake_warnings` 記録を個別に検証） | ユーザー判断②（辞書ベース検知＋allowlist 破棄の承認条件） |
| OI-2 | `docs/07_operation_guide.md` | **導入顧客側での法務確認を導入前提として明記**（検知辞書の網羅性の顧客法務レビュー・`retention_months` 保持期間の顧客要件確定） | ユーザー判断②（承認条件） |
| OI-3 | `docs/07_operation_guide.md` | **軸別 sufficiency の機械的足切り利用の禁止を利用条件として明記**（軸別評価値を閾値化して自動スクリーニングに転用する運用は本テンプレートの利用条件違反であることを、非断定契約（§2-5）の運用面の裏付けとして記載） | ユーザー判断④（承認条件） |
| OI-4 | `docs/07_operation_guide.md` | EU 域内上市時の provider/deployer 責任分界・適合性評価/登録（Art.49）の導入前提の明記（§4-1 で設計済み・運用ガイドへの転記） | §4-1（設計レビュー指摘対応時から継続） |

---

*Design v1.1 — 2026-07-06（設計レビュー反映: checkpoint_ttl_hours 72→120・Open Items 追加）*

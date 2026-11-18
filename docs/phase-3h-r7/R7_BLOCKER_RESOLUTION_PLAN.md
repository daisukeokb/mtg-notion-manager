# Phase 3H-R7 Blocker Resolution Plan

2026-11-18時点。R7の最終報告(2026-11-16)と、Evidence Closure & Blocker Resolution Planning(2026-11-17)の結果を、リポジトリ内の記録として残す。

**Phase 3H-R7 = BLOCKED / NOT_READY。MERGE NOT AUTHORIZED。PRODUCTION CUTOVER NOT AUTHORIZED。**

本文書は判断の材料であり、判断そのものではない。下記H1〜H9はすべて人間の判断事項で、R7の実装者・監査agentは代わりに判断しない。本文書のcommit、PRのmerge、CIのPASSはいずれも本番移行の承認ではない。

## 1. Quality Gates(G1〜G10)の現在の状態

| Gate | 状態 | 根拠 |
|---|---|---|
| G1 Source Integrity | PASS | `R7_BASELINE_AUDIT.md`。origin/main `09b651f` を基準に、変更は追加だけ(`src/`・CI・Error/Mutation Contractは不変) |
| G2 Bootstrap Trust Boundary | **UNVERIFIED** | 検査(`verify_bootstrap`)は実装・test済み。実runtimeのmanifestと、その独立に記録されたsha256が無い(B-D1) |
| G3 Launcher Isolation | PASS(dry-runの計画の範囲に限る) | `plan_launch` は計画を作るだけで実行しない(`EXECUTION_ENABLED = False`)。実行可能な起動経路はB-C23 |
| G4 Cutover Procedure | PASS(runbookと模擬) | `R7_CUTOVER_RUNBOOK.md` |
| G5 Rollback Viability | PASS(ローカルの切戻しに限る) | Notionへの書き込み後は条件付き(B-RB)。`R7_ROLLBACK_RUNBOOK.md` |
| G6 Regression & Contracts | PASS | `1251bbd` のローカル全体実行、PR #52のCI。`af20d5e` の3.14全体実行の1件失敗は失敗として記録(`R7_RESIDUAL_RISK_REGISTER.md` の Timing tests) |
| G7 GitHub Governance | **FAIL** | 必須approval 0、CODEOWNERSなし、collaborator 1名(`R7_GITHUB_GOVERNANCE_AUDIT.md`) |
| G8 Evidence Integrity | 修正中(D-1・D-2) | `970e297` の独立監査で、registerとEVIDENCE.jsonの矛盾(D-1)と、G2・G10の状態がリポジトリに記録されていないこと(D-2)が見つかった。本文書とregisterの訂正で修正する。修正後の判定は、修正を含むHEADの独立監査による(監査結果をそのHEADに書き込むと監査対象のSHAが変わるため、結果は本作業の最終報告に記録する) |
| G9 Independent Security Audit | PASS | 6回目(`1251bbd`)でPASS。`970e297` の証跡監査もPASS(major 0) |
| G10 Human Acceptance Readiness | **BLOCKED** | 下記4つのBlockerが未解消で、H1〜H7の判断が無い |

UNVERIFIED・FAIL・BLOCKEDはPASSではない。Blockerを例外として受け入れた場合も、該当gateの判定は変えない(例: H1で補償統制を受け入れても、G7はFAILのまま)。

## 2. R7がBLOCKEDである理由

- G2がUNVERIFIED: 実runtimeが検証されていない。
- G7がFAIL: mainへの変更にreviewが技術的に強制されず、検査script自体の信頼がこれに依存する。
- G10がBLOCKED: 下表の4つのCutover Blockerが未解消で、人間の判断(H1〜H7)が記録されていない。

R7の範囲(本番Notionへの接続禁止、既存runtimeの変更禁止、GitHub設定の変更禁止)では、これらを解消できない。

## 3. Cutover Blockerの状況と解決案

| ID | 状況(2026-11-17の再確認) | 解決案(推奨を先に記載) | 必要な作業 | 検証方法 | 人間の判断 |
|---|---|---|---|---|---|
| B-G1 | live設定は不変(ruleset "Protect main" active、bypassなし、必須approval 0、必須check test/lint/type-check)。GitHubは作成者自身のapprovalを数えないため、collaborator 1名で必須approvalを1以上にするとmergeできない | (b) GitHub外のreview(人間のHuman Acceptance記録+独立監査+実行前の検査script sha256照合)を補償統制として明示的に受け入れる / (a) 第2のreviewer、必須approval ≥ 1、CODEOWNERS、stale approvalのdismiss | (b) 承認記録の作成 / (a) GitHub設定の変更(別Work Unit・別承認) | (b) 承認記録にG-1と補償統制が書かれていること / (a) rulesetの読み取り再確認 | H1 |
| B-D1 | manifestを作るcommandが無い(runbook §4)。manifestの `app_root` は1つだけで、`.venv-py314-r5` の `.pth` の1件が `tools/generation_workspace` を指すため、`app_root = src/` ではfail closedになる(R7-8) | 次Work Unitでmanifest作成のsubcommand(`walk_tree` を再利用。`.env` を含むリポジトリ直下を `app_root` にしない)を追加し、`app_root` は (i) generation_workspaceを含まない本番専用venvを新規作成(推奨) / (ii) schemaを複数app rootへ拡張 | Cutover Safety Gateの再実行 → 直後にmanifest作成 → sha256をリポジトリ外に記録 → `bootstrap` | `bootstrap` がPASS(UNVERIFIEDではない)。あわせて、算出した `sys.path` と起動後の `sys.path` を隔離環境で比較(R7-2の残余) | H2・H3・H4 |
| B-C23 | launcherは無効化された計画だけ | (a) `plan_launch` の出力どおりの手動 `env -i` 起動。最初は読み取りcommand(`doctor` から)に限定 / (b) 実行可能なlauncher(`EXECUTION_ENABLED` の有効化、exec、起動直前の再検査でR7-3を閉じる。新たな監査が必要) | (a) 手順の承認 / (b) 実装・監査 | 合成の設定fileで、手動のcommandと計画の一致を確認 | H5 |
| B-RB | リポジトリにbackup・export機能は無い。重複統合系commandは削除APIを使わないが、`import` とタイトル更新は作成・上書きをする。runtimeの切戻しではNotion上のデータは戻らない | (a) 書き込みcommandの前に、両データベースのNotion側の復旧手段(workspace export、page history)を人間が確認する。または当面、読み取りcommandだけを承認する / (b) 読み取り専用のexport command(本番Notionの読み取りになるため別承認) | (a) 人間の確認と記録 / (b) 実装・別承認 | exportの日時と件数(内容は記録しない)が承認記録にあること | H6 |

## 4. Human Acceptance Decision Matrix

| # | 判断事項 | 選択肢(推奨を先に記載) | gateへの影響 |
|---|---|---|---|
| H1 | review governance(B-G1) | 補償統制の受け入れ / GitHubでの強制 | 前者: G7はFAILのまま例外として受け入れ / 後者: G7がPASSになりうる |
| H2 | runtimeのprovenance(R7-6、B-D1) | 受け入れ / 拒否 | G2の前提 |
| H3 | `app_root` の決め方 | generation_workspaceを含まない本番専用venv / 複数app root | G2の前提 |
| H4 | manifest hashの記録先 | リポジトリ外の、人間が選ぶ場所 | G2の前提 |
| H5 | 起動方法(B-C23) | 手動 `env -i`(読み取りから) / 実行可能なlauncher | G3の範囲 |
| H6 | Notionの復旧手段(B-RB) | 人間が確認 / export commandを作る | G5が無条件になる |
| H7 | 残リスクの受け入れと運用責任者 | `R7_RESIDUAL_RISK_REGISTER.md` の一覧を受け入れ / 拒否 | G10 |
| H8 | D-1・D-2の修正をR7 branchへ入れる | 承認 / 延期 | G8(2026-11-18に承認され、本文書で実施) |
| H9 | Production Cutover Authorization | runbook §2の別の承認記録 | — |

## 5. Production Cutover Authorizationに必要な証跡

1. 承認対象のcommit SHAと、そのCI結果。
2. Cutover Safety Gateの、最初からの再実行で全項目PASS(以前の結果を再利用しない)。
3. runtime archiveのsha256と、provenanceの受け入れ(H2)。
4. manifestのsha256(リポジトリ外に記録、H4)と、`bootstrap` の **PASS**(UNVERIFIEDではない)。
5. 実行する検査scriptのsha256と、承認記録の値の一致。
6. B-G1の扱い(H1)。
7. 承認された起動方法と、全preflightがPASSのdry-run計画(H5)。
8. Notionの復旧手段の確認、または読み取りcommandだけに限る範囲(H6)。
9. 切替前の記録: `.venv` のfingerprintと `pip freeze` のhash。
10. IDで特定した残リスクの受け入れと、運用責任者(H7)。
11. 承認記録そのもの: 承認者、日時、commit、runtime、各Blockerの扱い。

## 6. R7で閉じるものと次のWork Unit

- R7で閉じる(本番への接続なし): D-1(registerの訂正)、D-2(本文書)、最終SHA監査の記録(`docs/evidence/phase-3h-r7/EVIDENCE.json` の `final_sha_audit`)。これらを閉じてもR7はBLOCKEDのまま。
- 次のWork Unit(推奨範囲「R8 Runtime Attestation & Read-only Cutover Preparation」): manifest作成のsubcommandとtest、本番専用venv(H3で選んだ場合)、Cutover Safety Gateの再実行・manifest・hash、`bootstrap` のPASSと起動後の `sys.path` の比較、合成の設定fileによる手動 `env -i` 起動のdry-run。
- 別のWork Unit: GitHub設定の変更(H1で強制を選んだ場合)、Notionのexport command(H6でbを選んだ場合)、timing依存testの修正。

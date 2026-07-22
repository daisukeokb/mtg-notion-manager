# Normalization Ledger — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

この文書は、Patch適用と正規化判断の監査証跡だけを所有する。State/Event定義そのものは[state_catalog.md](state_catalog.md)・[event_catalog.md](event_catalog.md)を正本とし、本文書はそこへ至った判断過程を記録する。

## 監査単位と表記規則

- 監査単位は`Target ID × Changed Field`を1件とする。入力(`02-state-event-catalog-patch-1.txt` / `03-state-event-catalog-patch-2.txt`)が明示する`Changed Field`(または`Changed Fields`列挙)を、原則そのまま1フィールド=1行として記録する。
- Event Importance分類表のように、入力側が範囲(例: `EV-ADM-001–004`)で1行にまとめている場合は、Ledgerも1行として記録する(個別Event ID単位へは分解しない)。
- 同一`Target ID × Changed Field`が複数Patchで更新された場合、各Patch適用時点の判断を別行として記録し、後続行の`Resolution`に`(supersedes NL-xxx)`を付記する。最終的な採用値は`state_catalog.md`／`event_catalog.md`の対応箇所を正とする。
- Base単独で保持され、いかなるPatchも対象としなかったState/Event IDは、個別行を起こさず本文書末尾の「Base Retainedの範囲」にID一覧として記録する(全項目を行に展開すると監査単位が膨大になるため)。
- Handoff(`04-transition-failure-handoff.md`)は、Architecture Baselineの主入力であり(architecture_baseline.mdへ直接反映)、Catalogフィールド単位の新規変更は含まない。Handoff §6の記述をPatch 2適用後のCatalogと突き合わせた確認結果を1行(NL-086)として記録する。

`Resolution`列の値:

```text
Base Retained
Patch 1 Applied
Patch 2 Applied
Handoff Constraint Applied
Deprecated
Input Conflict
```

---

## Patch 1 — State Catalog Patch (Target: `01` × `02`)

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-001 | 02 | ST-ADM-001 | state_catalog.md | Recovery Owner | N/A during normal progression; Admission Recovery only when unknown | Admission Recovery | Patch 1 Applied | none | — |
| NL-002 | 02 | ST-ADM-004 | state_catalog.md | Recovery Owner | N/A; Post-commit Audit Repair for derived-artifact issues | 条件別: 整合時Post-commit Audit Repair／不整合時Admission Recovery | Patch 1 Applied | none | — |
| NL-003 | 02 | ST-CLM-002 | state_catalog.md | Recovery Owner | Triggering recovery owner / Open | 3段階resolution order(Certificate不在→Admission Recovery / Start Boundary未記録→Startup Recovery / それ以外→Execution Recovery) | Patch 1 Applied | none | SM-CLM-002 |
| NL-004 | 02 | ST-GEN-002 | state_catalog.md | Recovery Owner | Open: SM-GEN-002 | Admission Recovery(Admission atomic commit内のgeneration確定に限定) | Patch 1 Applied | none | SM-GEN-002 |
| NL-005 | 02 | ST-GEN-002 | open_questions.md | Open Questions(narrowing) | SM-GEN-002(素の記載) | Admission Commit外のgeneration更新経路の有無へ限定 | Patch 1 Applied | none | SM-GEN-002 |
| NL-006 | 02 | ST-STP-001 | state_catalog.md | Recovery Owner | N/A / Orchestrator-led progression | Execution Startup Recovery | Patch 1 Applied | none | — |
| NL-007 | 02 | ST-EXE-001 | state_catalog.md | Candidate Name | Execution Start Boundary Established (Operation Path) | Execution Started / In Progress | Patch 1 Applied | none | — |
| NL-008 | 02 | ST-EXE-001 | state_catalog.md | Related Domains | (フィールドなし) | Startup, No-op, Dispatch | Patch 1 Applied | none | — |
| NL-009 | 02 | ST-EXE-001 | state_catalog.md | Allowed Behavior | (未明示) | Execution Recovery ownership; Dispatch Permit acquisition; continued Operation execution | Patch 1 Applied | none | — |
| NL-010 | 02 | ST-EXE-001 | state_catalog.md | Forbidden Behavior | (未明示) | Startup Recoveryへの復帰禁止; generation/ownership不一致下の継続禁止 | Patch 1 Applied | none | — |
| NL-011 | 02 | ST-EXE-002 | state_catalog.md | Draft Status | Proposed | Deprecated draft alias(ST-EXE-001へmerge) | Deprecated | none | — |
| NL-012 | 02 | ST-EXE-004 | state_catalog.md | Draft Status | Proposed | Open | Patch 1 Applied | none | SM-EXE-001 |
| NL-013 | 02 | ST-EXE-004 | state_catalog.md | Lifecycle Kind | Terminal in base draft | Indeterminate | Patch 1 Applied | none | SM-EXE-001 |
| NL-014 | 02 | ST-EXE-004 | state_catalog.md | Recovery Classification | (フィールドなし) | Recovery Required | Patch 1 Applied | none | SM-EXE-001 |
| NL-015 | 02 | ST-EXE-004 | state_catalog.md | Allowed Behavior | (未明示) | 耐久的状態の検査; Binding/Fencing/Generation再評価; 承認済みRecovery Design actionのみ | Patch 1 Applied | none | SM-EXE-001 |
| NL-016 | 02 | ST-EXE-004 | state_catalog.md | Forbidden Behavior | (未明示) | stale-owner継続; 自動復帰; 自動再送; ownership変更のみでのPermit再発行; Startup Recovery復帰禁止 | Patch 1 Applied | none | SM-EXE-001 |
| NL-017 | 02 | ST-DSP-002 | state_catalog.md | Exit Evidence | (未明示) | Normal/Unknown 2分岐(初版) | Patch 1 Applied(supersedes: none / superseded by NL-067) | none | SM-DSP-001 |
| NL-018 | 02 | ST-DSP-002 | state_catalog.md | Allowed Behavior | (未明示) | pre-send guards評価 + 12項目Guard最低条件(初版) | Patch 1 Applied(superseded by NL-065) | none | — |
| NL-019 | 02 | ST-DSP-002 | state_catalog.md | Forbidden Behavior | (未明示) | Claimed=送信許可ではない; Confirmed前のsend confirmed扱い禁止(初版) | Patch 1 Applied(superseded by NL-066) | none | — |
| NL-020 | 02 | ST-DSP-002 | state_catalog.md | Forbidden Interpretation | (未明示) | Claimed≠全guard成立; Claim取得≠送信発生 | Patch 1 Applied(維持、Patch2で上書きなし) | none | — |
| NL-021 | 02 | ST-DSP-006 | state_catalog.md | State Kind | Derived | Open — Durable classification mechanism unresolved | Patch 1 Applied | none | SM-DSP-001 |
| NL-022 | 02 | ST-DSP-006 | state_catalog.md | Entry Evidence | (未明示) | 明示的durable classification Eventまたは一意導出; 不在/timeout等単独では不可 | Patch 1 Applied | none | SM-DSP-001 |
| NL-023 | 02 | ST-DSP-007 | state_catalog.md | State Kind | Derived | Open — Durable classification mechanism unresolved | Patch 1 Applied | none | SM-DSP-001 |
| NL-024 | 02 | ST-DSP-007 | state_catalog.md | Entry Evidence | (未明示) | Confirmed成立後の明示的Eventまたは一意導出; 単独要因では不可 | Patch 1 Applied | none | SM-DSP-001 |

> 注記(CTOセルフレビュー指摘への対応): `EV-ADM-002`のNL-026〜029は、Patch 1原文が`Transaction Boundary`・`Idempotency Identity`・`Preconditions`・`Durable Fact`を1つの結合した説明として記述しており、フィールドごとの完全に独立した文への分割ではない。本Ledgerでの4行分割は監査可能性のための編集上の分解であり、Patch 1原文の逐語的な1文1フィールド対応を意味しない。

## Patch 1 — Event Catalog Patch

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-025 | 02 | EV-ADM-001 | event_catalog.md | Idempotency Identity | Attempt + Binding | 9要素(attempt/scope/binding/plan identity・digest/environment/database/authorization scope/execution identity) | Patch 1 Applied | none | — |
| NL-026 | 02 | EV-ADM-002 | event_catalog.md | Transaction Boundary | (フィールドなし) | Single Transaction Profile内の単一atomic commit | Patch 1 Applied | none | — |
| NL-027 | 02 | EV-ADM-002 | event_catalog.md | Idempotency Identity | (フィールドなし) | EV-ADM-001と同一9要素 | Patch 1 Applied | none | — |
| NL-028 | 02 | EV-ADM-002 | event_catalog.md | Preconditions | (フィールドなし) | atomic commit境界内でのAdmission facts成立 | Patch 1 Applied | none | — |
| NL-029 | 02 | EV-ADM-002 | event_catalog.md | Durable Fact | (暗黙にST-ADM-004) | post-commit artifact非依存; 重複Certificate禁止を明記 | Patch 1 Applied | none | — |
| NL-030 | 02 | EV-EXE-001 | event_catalog.md | Resulting Evidence | (暗黙) | ST-EXE-001確立 + Execution Recoveryへ不可逆移管 | Patch 1 Applied | none | — |
| NL-031 | 02 | EV-EXE-004 | event_catalog.md | Recovery Ownership Effect | execution終了の起点 | Execution Recoveryへの耐久的制御移管; Terminal未確立 | Patch 1 Applied | none | SM-EXE-001 |
| NL-032 | 02 | EV-DSP-002 | event_catalog.md | Preconditions | Permit exists; consumer claim succeeds | 同上を明確化(Permit存在+single consumer claim成立) | Patch 1 Applied | none | — |
| NL-033 | 02 | EV-DSP-002 | event_catalog.md | Forbidden Inference | Claim proves send succeeded | 3項目(Claim成功/全guard成立/送信発生の推論禁止)(初版) | Patch 1 Applied(superseded by NL-077) | none | — |
| NL-034 | 02 | EV-DSP-003 | event_catalog.md | Preconditions | external send implementation reports approved boundary | 全pre-send guards成立 + invocation実行済み | Patch 1 Applied(維持) | none | EXE-DSP-003 |
| NL-035 | 02 | EV-DSP-003 | event_catalog.md | Resulting Evidence | ST-DSP-003; boundary between Indeterminate/Outcome Unknown | ST-DSP-003確立 + 境界越え + does-not-prove一覧(初版) | Patch 1 Applied(superseded by NL-078) | none | EXE-DSP-003 |
| NL-036 | 02 | EV-DSP-003 | event_catalog.md | Forbidden Inference | Confirmed can be inferred before it is durable | pre-send guard扱い禁止; 耐久化前の推論禁止 | Patch 1 Applied(維持) | none | — |
| NL-037 | 02 | EV-DSP-003 | event_catalog.md | Open Questions | (フィールドなし) | EXE-DSP-003登録、最終承認前必須(初版) | Patch 1 Applied(superseded by NL-079) | none | EXE-DSP-003 |
| NL-038 | 02 | EV-OWN-001 | event_catalog.md | Event Importance | (フィールドなし) | Open Classification Candidate | Patch 1 Applied | none | EXE-OWN-001 |
| NL-039 | 02 | EV-OWN-002 | event_catalog.md | Event Importance | (フィールドなし) | Open Classification Candidate | Patch 1 Applied | none | EXE-OWN-002 |
| NL-040 | 02 | EV-ADM-001–004 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-041 | 02 | EV-CLM-001–004 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-042 | 02 | EV-GEN-001 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-043 | 02 | EV-STP-001 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-044 | 02 | EV-EXE-001/002 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-045 | 02 | EV-EXE-003/004 | event_catalog.md | Event Importance | (フィールドなし) | Optional Derived Candidate | Patch 1 Applied | none | SM-EXE-001, SM-EXE-002 |
| NL-046 | 02 | EV-DSP-001–005 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-047 | 02 | EV-NOP-001 | event_catalog.md | Event Importance | (フィールドなし) | Mandatory Durable Event | Patch 1 Applied | none | — |
| NL-048 | 02 | EV-NOP-002 | event_catalog.md | Event Importance | (フィールドなし) | Open Classification Candidate | Patch 1 Applied | none | SM-NOP-001 |

## Patch 1 — Open Question Register Corrections

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-049 | 02 | SM-CLM-002 | open_questions.md | Question/Default構造化 | 素の1行記載 | Owner Resolution Rule + Current Catalog Blocker: No を構造化 | Patch 1 Applied | none | SM-CLM-002 |
| NL-050 | 02 | SM-GEN-002 | open_questions.md | Question/Default構造化 | 素の1行記載 | Fail Closed default + blocker: No(初版) | Patch 1 Applied(superseded by NL-083) | none | SM-GEN-002 |
| NL-051 | 02 | SM-EXE-001 | open_questions.md | Question/Default構造化 | 素の1行記載 | Terminal条件等5論点を列挙 + blocker: No | Patch 1 Applied | none | SM-EXE-001 |
| NL-052 | 02 | SM-EXE-002 | open_questions.md | Question構造化 | 素の1行記載 | Question文の明確化 | Patch 1 Applied | none | SM-EXE-002 |
| NL-053 | 02 | SM-NOP-001 | open_questions.md | Question構造化 | 素の1行記載 | Question文の明確化 | Patch 1 Applied | none | SM-NOP-001 |
| NL-054 | 02 | SM-DSP-001 | open_questions.md | Question/Default構造化 | 素の1行記載 | Candidate A/B + Fail Closed default(初版) | Patch 1 Applied(superseded by NL-080) | none | SM-DSP-001 |
| NL-055 | 02 | EXE-DSP-003 | open_questions.md | Open Question登録 | (未登録) | 最終承認前必須として登録(初版) | Patch 1 Applied(superseded by NL-082) | none | EXE-DSP-003 |
| NL-056 | 02 | EXE-OWN-001 | open_questions.md | Open Question登録/non-blocker注記 | (未登録) | non-blocker注記付きで登録 | Patch 1 Applied | none | EXE-OWN-001 |
| NL-057 | 02 | EXE-OWN-002 | open_questions.md | Open Question登録/non-blocker注記 | (未登録) | non-blocker注記付きで登録 | Patch 1 Applied | none | EXE-OWN-002 |

## Patch 1 — Blocker Report

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-058 | 02 | [Catalog-wide] | architecture_baseline.md / state_catalog.md / event_catalog.md | Blocker Status | Base Section H: Pass with Open Questions(項目別) | Architecture blocker: none / State Machine Design draft blocker: none(Patch1適用後の確認) | Patch 1 Applied | none | — |

---

## Patch 2 — State Catalog Patch

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-059 | 03 | ST-DSP-001 | state_catalog.md | Facts proved/not proved | (未整理) | proved: Permit uniqueness/atomic idempotent/lifecycle突入; not proved: consumer選択/Claim存在/送信許可/送信発生 | Patch 2 Applied | none | — |
| NL-060 | 03 | ST-DSP-001 | state_catalog.md | Single-consumer establishment point | (未定義) | 未確立 | Patch 2 Applied | none | — |
| NL-061 | 03 | ST-DSP-001 | state_catalog.md | Allowed Behavior | (未明示) | Invocation Claim取得の試行のみ | Patch 2 Applied | none | — |
| NL-062 | 03 | ST-DSP-001 | state_catalog.md | Forbidden Behavior | (未明示) | Permitのみでの送信/Claimなし送信/追加Permit発行/状態不明時送信の禁止 | Patch 2 Applied | none | — |
| NL-063 | 03 | ST-DSP-002 | state_catalog.md | Facts proved/not proved | (未整理、Patch1は行動規範中心) | proved: single consumer確立/そのExecutorのみguard評価可; not proved: 全guard成立/送信開始/Confirmed存在 | Patch 2 Applied | none | — |
| NL-064 | 03 | ST-DSP-002 | state_catalog.md | Single-consumer establishment | (Patch1未定義) | ST-DSP-002耐久成立時点 | Patch 2 Applied | none | — |
| NL-065 | 03 | ST-DSP-002 | state_catalog.md | Allowed Behavior | NL-018(pre-send guards評価 + 12項目) | guard評価 + guard成立後のsingle-consumer Executorによる送信開始 + at-most-once規範明記 | Patch 2 Applied(supersedes NL-018) | none | — |
| NL-066 | 03 | ST-DSP-002 | state_catalog.md | Forbidden Behavior | NL-019(2項目) | 10項目(validation未完了送信/複数回送信開始/Permit・Claim不明送信/Binding不一致/Fencing不一致/generation不一致/ownership generation不一致/Recovery Locked・Terminal下送信/Start Boundary前送信/stale owner送信) | Patch 2 Applied(supersedes NL-019) | none | — |
| NL-067 | 03 | ST-DSP-002 | state_catalog.md | Exit Evidence | NL-017(Normal/Unknown初版) | Normal: Confirmed耐久化まで詳細化。Unknown: SM-DSP-001選定のevidence契約のみで分類、crash単独でのDurable State遷移否定 | Patch 2 Applied(supersedes NL-017) | none | SM-DSP-001 |
| NL-068 | 03 | ST-GEN-002 | state_catalog.md | Candidate Name | Execution Scope Generation Commit Indeterminate(Patch1時点) | Admission-Time Execution Scope Generation Commit Indeterminate | Patch 2 Applied | none | SM-GEN-002 |
| NL-069 | 03 | ST-GEN-002 | state_catalog.md | Scope | (未定義) | Admission atomic commit境界内のgeneration確定のみ; 境界外はOpen | Patch 2 Applied | none | SM-GEN-002 |
| NL-070 | 03 | ST-GEN-002 | state_catalog.md | State Kind | Derived | Open — Durable classification mechanism unresolved | Patch 2 Applied | none | SM-GEN-002 |
| NL-071 | 03 | ST-GEN-002 | state_catalog.md | Entry Evidence | (未明示) | Admission atomic commitまたはRecovery結果からの承認済みPersistent Evidence契約 | Patch 2 Applied | none | SM-GEN-002 |
| NL-072 | 03 | ST-GEN-002 | state_catalog.md | Allowed Behavior | (未明示) | Admission Recoveryによるatomic commit結果検証のみ | Patch 2 Applied | none | SM-GEN-002 |
| NL-073 | 03 | ST-GEN-002 | state_catalog.md | Forbidden Behavior | (未明示) | unknownを旧generation扱い禁止/自動retry禁止/Admission owner ruleの境界外適用禁止 | Patch 2 Applied | none | SM-GEN-002 |

## Patch 2 — Event Catalog Patch

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-074 | 03 | EV-DSP-001 | event_catalog.md | Durable Fact | (暗黙にST-DSP-001) | Operation-level Permit uniqueness確立 | Patch 2 Applied | none | — |
| NL-075 | 03 | EV-DSP-001 | event_catalog.md | Forbidden Inference | Permit proves send occurred(単一) | 3項目(consumer確立/送信許可/送信発生の推論禁止) | Patch 2 Applied | none | — |
| NL-076 | 03 | EV-DSP-002 | event_catalog.md | Durable Fact | (フィールドなし、ST-DSP-002参照のみ) | Permit使用Write Executorがsingle consumerとして確立 | Patch 2 Applied | none | — |
| NL-077 | 03 | EV-DSP-002 | event_catalog.md | Forbidden Inference | NL-033(3項目) | 4項目(全guard成立/送信発生/Claim=成功/最低条件充足の推論禁止) | Patch 2 Applied(supersedes NL-033) | none | — |
| NL-078 | 03 | EV-DSP-003 | event_catalog.md | Resulting Evidence | NL-035(初版、does-not-prove含む) | 簡潔化: 承認境界越えのみ証明 + does-not-prove一覧維持 | Patch 2 Applied(supersedes NL-035) | none | EXE-DSP-003 |
| NL-079 | 03 | EV-DSP-003 | event_catalog.md | Open Questions | NL-037(初版) | 明示的blocker構造(Current Catalog Blocker: No / Blocks Dispatch Transition Final Approval: Yes) + 新規Event追加禁止の明記 | Patch 2 Applied(supersedes NL-037) | none | EXE-DSP-003 |

## Patch 2 — Open Question Corrections

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-080 | 03 | SM-DSP-001 | open_questions.md | Fail Closed Default(拡張) | NL-054(初版) | クラッシュ窓を分類対象へ明記; 未送信・失敗側への丸め禁止等6項目のFail Closed defaultへ拡張 | Patch 2 Applied(supersedes NL-054) | none | SM-DSP-001 |
| NL-081 | 03 | REC-DSP-002 | open_questions.md | Question/Default(新規実質化) | Base: 見出しのみ | 未送信証明Evidence + 送信開始後クラッシュ窓の解決要件を追加 | Patch 2 Applied | none | REC-DSP-002 |
| NL-082 | 03 | EXE-DSP-003 | open_questions.md | Requirements(拡張) | NL-055(初版) | 4要件(送信開始後のみ成立/承認Writerで記録可能/外部完了を主張しない/2分類を分離) + blocker構造の明確化 | Patch 2 Applied(supersedes NL-055) | none | EXE-DSP-003 |
| NL-083 | 03 | SM-GEN-002 | open_questions.md | Question(3設問へ分割) | NL-050(単一質問、初版) | 3設問(evidence根拠/境界外経路有無/存在時の定義)へ分割 | Patch 2 Applied(supersedes NL-050) | none | SM-GEN-002 |

---

## Handoff Constraint適用

| Ledger ID | Source | Target ID | Target Document | Changed Field | Base Value Summary | Applied Value Summary | Resolution | Conflict | Related Open Question |
|---|---|---|---|---|---|---|---|---|---|
| NL-084 | 01 | SM-CLM-001 | open_questions.md | Open Question登録(判断) | Base Section Eにのみ存在。Handoff §7の「主なOpen Question」一覧には非掲載 | Handoffでの非掲載は解消の明示ではないため、成功側の仮定を避けOpenのまま保持 | Base Retained | none | SM-CLM-001 |
| NL-085 | 01 | SM-GEN-001 | open_questions.md | Open Question登録(判断) | Base Section Eにのみ存在。Handoff §7の「主なOpen Question」一覧には非掲載 | 同上の理由でOpenのまま保持 | Base Retained | none | SM-GEN-001 |
| NL-086 | 04 | [Catalog-wide] | catalog_consistency_review.md | Handoff §6 整合確認 | — | Handoff §6(Catalog上の重要判断)をPatch 2適用後の最終Catalogと突合し、乖離なしを確認(Permit uniqueness/single-consumer分離、Fencing Conflict分類、ST-EXE-001統合、Admission Idempotency 9要素、ST-GEN-002範囲限定、Dispatch Indeterminate/Outcome Unknown二分類のいずれも一致) | Handoff Constraint Applied | none | — |

---

## Base Retainedの範囲(Patch非対象、行未展開)

以下のState/Event IDは、いずれのPatchからも明示的Target IDとして参照されず、Base draftの内容をそのまま維持した(Event ImportanceのみPatch 1の範囲表(NL-040〜048)で扱われたEventも含む)。

**State(Base Retained、Patch対象外)**: `ST-ADM-002`, `ST-ADM-003`, `ST-ADM-005`, `ST-CLM-001`, `ST-CLM-003`, `ST-GEN-001`, `ST-OWN-001`, `ST-OWN-002`, `ST-OWN-003`, `ST-STP-002`, `ST-EXE-003`, `ST-EXE-005`, `ST-DSP-003`, `ST-DSP-004`, `ST-DSP-005`, `ST-NOP-001`, `ST-NOP-002`(17件)

**Event(個別フィールドのPatch対象外。Event Importance欄のみNL-040〜048で扱済み)**: `EV-ADM-003`, `EV-ADM-004`, `EV-CLM-001`〜`004`, `EV-GEN-001`, `EV-STP-001`, `EV-EXE-002`, `EV-EXE-003`, `EV-DSP-004`, `EV-DSP-005`, `EV-NOP-001`, `EV-NOP-002`

---

## Normalization Summary

```text
Ledger総件数: 86件(NL-001〜NL-086)
Patch 1 Applied: 58件(NL-001〜NL-058)
Patch 2 Applied: 25件(NL-059〜NL-083)
Handoff Constraint Applied: 1件(NL-086)
Base Retained(判断付き): 2件(NL-084, NL-085)
Deprecated: 1件(NL-011)
Input Conflict: 0件
```

Patch 1適用件数のうち、後続Patch 2によって上書き(supersede)されたのは9件(NL-017, NL-018, NL-019, NL-033, NL-035, NL-037, NL-050, NL-054, NL-055)。内訳はPatch 2ログの`supersedes`注記を参照。上書きされた行も履歴として保持し、削除しない。

---

## Human Review Correction Pass(NL-087以降)

`NL-001`〜`NL-086`は削除・改番・並べ替え・上書きを一切行っていない。本セクションはCanonicalization Review Package v1の人間レビューで確認された不整合(FINDING-001〜005)に対する補正だけを、新規行として追記する。

- Source(全行共通): `Human Review of Canonicalization Review Package v1`
- 監査単位: `Target Document × Target IDまたはReview Section × Changed Field`(1行1Field)
- Resolution値: `Review Correction Applied`(内容を実際に修正)、`Defect Status Clarified`(既存Defectの確定度表現を明確化。Defect自体はResolvedへ変更しない)

| Ledger ID | Source | Target Document | Target ID / Review Section | Changed Field | Before | After | Resolution | Related Finding |
|---|---|---|---|---|---|---|---|---|
| NL-087 | Human Review v1 | catalog_consistency_review.md | Catalog件数 / 検証結果一覧#1 | State Count | 27 | 29(`ST-EXE-002` Deprecated draft alias含む。Active excluding Deprecated: 28) | Review Correction Applied | FINDING-001 |
| NL-088 | Human Review v1 | catalog_consistency_review.md | Catalog件数 / 検証結果一覧#2 | Event Count | 24 | 23 | Review Correction Applied | FINDING-001 |
| NL-089 | Human Review v1 | catalog_consistency_review.md | Catalog件数 | Open Question Count | (件数の明示なし) | 25(必須23件 + Base由来`SM-CLM-001`/`SM-GEN-001`の2件)を明示 | Review Correction Applied | FINDING-001 |
| NL-090 | Human Review v1 | catalog_consistency_review.md | 検証結果一覧#4(Entry Evidence Review) | 分類・結果 | 「全State定義済み」/ `No Issue` | `ST-DSP-001/003/004/005`のState固有Entry Evidence未確定を反映した`Normalization Defect`(ND-002参照) | Review Correction Applied | FINDING-002 |
| NL-091 | Human Review v1 | defect_resolution_proposal.md(Review Package v2、Repository外文書) | ND-002 | Classification | `New Architecture Decision Required` | `Input Insufficient` | Review Correction Applied | FINDING-003 |
| NL-092 | Human Review v1 | defect_resolution_proposal.md(Review Package v2、Repository外文書) | ND-002 | New Architecture Decision Required | 矛盾した併記(項目名としての`New Architecture Decision Required`と値`No`が並記され読み取りづらい状態) | `No`へ統一 | Review Correction Applied | FINDING-003 |
| NL-093 | Human Review v1 | state_catalog.md | ST-EXE-001 | Exit Evidence | 確定的表現(ST-EXE-003/004/005への遷移を確定済みのように記述) | `Open`(Operation単位／Execution全体単位の粒度未確定を明記、ND-003参照) | Review Correction Applied | FINDING-004 |
| NL-094 | Human Review v1 | catalog_consistency_review.md | Blocker分類 / ND-001 | Blocks Canonical Promotion | (未記載) | `Yes` | Review Correction Applied | FINDING-005 |
| NL-095 | Human Review v1 | catalog_consistency_review.md | Blocker分類 / ND-002 | Blocks Canonical Promotion | (未記載) | `Yes` | Review Correction Applied | FINDING-005 |
| NL-096 | Human Review v1 | catalog_consistency_review.md | Blocker分類 / ND-003 | Blocks Canonical Promotion | (未記載) | `Yes` | Review Correction Applied | FINDING-005 |
| NL-097 | Human Review v1 | state_catalog.md | ST-CLM-001 | Exit Evidence | 未定義(フィールド自体が存在しない) | `Open`として明示(ND-001参照) | Defect Status Clarified | Evidence clarification |
| NL-098 | Human Review v1 | state_catalog.md | ST-DSP-001 | Entry Evidence | `Unchanged`表記のみ(実際は未定義値への参照) | 未確定を`Open`として明示(ND-002参照) | Defect Status Clarified | Evidence clarification |
| NL-099 | Human Review v1 | state_catalog.md | ST-DSP-001 | Exit Evidence | `Unchanged`表記のみ(実際は未定義値への参照) | 未確定を`Open`として明示(ND-002参照) | Defect Status Clarified | Evidence clarification |
| NL-100 | Human Review v1 | state_catalog.md | ST-DSP-003 | Entry Evidence | フィールド自体が存在しない | 未確定を`Open`として明示(ND-002、`EXE-DSP-003`参照) | Defect Status Clarified | Evidence clarification |
| NL-101 | Human Review v1 | state_catalog.md | ST-DSP-003 | Exit Evidence | フィールド自体が存在しない | 未確定を`Open`として明示(ND-002参照) | Defect Status Clarified | Evidence clarification |
| NL-102 | Human Review v1 | state_catalog.md | ST-DSP-004 | Entry Evidence | フィールド自体が存在しない | 未確定を`Open`として明示(ND-002参照) | Defect Status Clarified | Evidence clarification |
| NL-103 | Human Review v1 | state_catalog.md | ST-DSP-004 | Exit Evidence | フィールド自体が存在しない | 未確定を`Open`として明示(ND-002参照) | Defect Status Clarified | Evidence clarification |
| NL-104 | Human Review v1 | state_catalog.md | ST-DSP-005 | Entry Evidence | フィールド自体が存在しない | 未確定を`Open`として明示(ND-002参照) | Defect Status Clarified | Evidence clarification |
| NL-105 | Human Review v1 | state_catalog.md | ST-DSP-005 | Exit Evidence | フィールド自体が存在しない | `Terminal per Dispatch`であるため適用外(Not applicable)と明示。既存分類の明文化であり新規Transition Decisionではない | Defect Status Clarified | Evidence clarification |
| NL-106 | Human Review v1 | catalog_consistency_review.md | Blocker分類 / ND-001 | Blocks Transition Specification | (未記載) | `Yes — Claim関連Transition` | Review Correction Applied | FINDING-005 |
| NL-107 | Human Review v1 | catalog_consistency_review.md | Blocker分類 / ND-002 | Blocks Transition Specification | (未記載) | `Yes — Transition Specification全体` | Review Correction Applied | FINDING-005 |
| NL-108 | Human Review v1 | catalog_consistency_review.md | Blocker分類 / ND-003 | Blocks Transition Specification | (未記載) | `Yes — Execution関連Transition` | Review Correction Applied | FINDING-005 |
| NL-109 | Human Review v1 | catalog_consistency_review.md | 総合結果 | Blocker軸の表現 | 「3件ともCatalog blockerではない」「いずれも非blocking」 | `Architecture blocker`／`Draft作成blocker`／`Canonical promotion blocker`／`Transition blocker`を分離し、Canonical PromotionはND-001/002/003によりblocked、Transition Specificationは全体としてnot permittedと明記 | Review Correction Applied | FINDING-005 |
| NL-110 | Human Review v1 | source_provenance.md | Human Review Correction Pass(新規セクション) | セクション追加 | (セクションなし) | Review Source、v1 ZIP SHA-256、Finding一覧、修正対象4文書、Defect Clarified状況等を末尾へ追記 | Review Correction Applied | 全Finding共通 |

### Ledger Summary(Human Review Correction Pass)

```text
Original normalization entries: 86
Human review correction entries: 24(NL-087〜NL-110)
Total ledger entries: 110
Patch 1 application count: 変更なし(58件、NL-001〜NL-058)
Patch 2 application count: 変更なし(25件、NL-059〜NL-083)
Handoff constraint count: 変更なし(1件、NL-086)
Resolved Normalization Defect: 0
Clarified Normalization Defect: 3(ND-001, ND-002, ND-003)
```

---

## Review Package v2 Human Review Correction Pass(NL-111以降)

`NL-001`〜`NL-110`は削除・改番・並べ替え・上書きを一切行っていない。本セクションはCanonicalization Review Package v2の人間レビュー(V2-FINDING-001〜003)に対する補正だけを、新規行として追記する。ND-001／ND-002／ND-003に残っていた古い解決時期・承認主体の説明を、既存のBlocker分類([catalog_consistency_review.md](catalog_consistency_review.md))と一致させるための補正であり、Defectの意味やEntry/Exit Evidenceの確定度は変更していない。

- Source(全行共通): `Human Review of Canonicalization Review Package v2`
- Resolution(全行共通): `Review Correction Applied`
- 監査単位: `Target Document × Target Section × Changed Field`(1行1Field)

| ID | Target Document | Target Section | Changed Field | Before | After | Related Finding |
|---|---|---|---|---|---|---|
| NL-111 | catalog_consistency_review.md | ND-001 | Resolution Timing | Transition Specification作成時 | Canonical昇格前の別作業。Transitionへ先送りしない | V2-FINDING-001 |
| NL-112 | catalog_consistency_review.md | ND-002 | Current Catalog Representation | ドメインprose参照のみ | State固有EvidenceをOpenとして明示済み | V2-FINDING-002 |
| NL-113 | catalog_consistency_review.md | ND-002 | Resolution Timing | Transition Specification作成時 | Canonical昇格・Transition開始前の別作業 | V2-FINDING-002 |
| NL-114 | catalog_consistency_review.md | ND-002 | Classification and Approval | Architecture Review | Input Insufficient、Cross-Domain Review、User Decision | V2-FINDING-002 |
| NL-115 | catalog_consistency_review.md | ND-003 | Resolution Timing | Import作業の範囲外とのみ記載 | Canonical昇格前の別作業。Transitionへ先送りしない | V2-FINDING-003 |
| NL-116 | source_provenance.md | Review Provenance | Review Package v2 Correction Pass | 記録なし | v2 Hash、Finding、修正範囲、意味変更なしを記録 | V2-FINDING-001〜003 |

### Ledger Summary(Review Package v2 Human Review Correction Pass)

```text
Original normalization entries: 86
Review Package v1 corrections: 24
Review Package v2 corrections: 6
Total ledger entries: 116
Resolved Normalization Defects: 0
Clarified Normalization Defects: 3(ND-001, ND-002, ND-003)
```

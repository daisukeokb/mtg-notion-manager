# Open Questions — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

この文書は、`Review-Governed Limited Apply`の未解決事項の詳細だけを所有する。他文書はOpen Question IDだけを参照し、詳細を複製しない。

同一IDが複数入力で更新されている場合は最新のAfter(Handoff → Patch 2 → Patch 1 → Base の優先順で、内容が競合しない限り最も詳細な記述)を採用した。入力優先順位・適用判断は[normalization_ledger.md](normalization_ledger.md)を参照する。

Open Questionが残っていても、Fail Closed動作と影響範囲が明示されていれば、Catalog Draftは成立する(Catalog blockerではない)。ただし個別に「最終承認前に解決必須」と明記された項目は、その成果物の最終承認条件である。

---

## 凡例

- **Current Catalog Blocker**: 現在のState/Event Catalog Draftの承認を妨げるか。
- **Blocks Current Draft**: 本Canonical Design Input Import Draftの成立を妨げるか(本文書ではCatalog blocker欄と同義として扱う。個別に明記がない項目は「入力に明示なし」とし、Base/Patchの全体自己レビュー結果である`Pass with Open Questions`を根拠に推測で「No」と断定しない)。
- **Source**: この項目の内容がどの入力ファイル由来かを示す(Base=01, Patch1=02, Patch2=03, Handoff=04)。

---

## State Machine Design

### SM-CLM-001

- Question: Active Execution Claimの確立・遷移に関するDecision Makerは誰か。
- Primary Owner: State Machine Design
- Dependencies: なし(入力に明記なし)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: 入力に個別記載なし(Base全体のCatalog自己レビューは`Pass with Open Questions`)
- Blocks Current Draft: 入力に明記なし
- Resolution Needed Before: 入力に明記なし
- Related State: `ST-CLM-001`
- Related Event: `EV-CLM-001`
- Source: Base(01) Section E

### SM-GEN-001

- Question: Execution Scope Generation確定に関するDecision Makerは誰か。
- Primary Owner: State Machine Design
- Dependencies: なし(入力に明記なし)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: 入力に個別記載なし
- Blocks Current Draft: 入力に明記なし
- Resolution Needed Before: 入力に明記なし
- Related State: `ST-GEN-001`
- Related Event: `EV-GEN-001`
- Source: Base(01) Section E

### SM-CLM-002

- Question: Claim Recovery LockedのOwner Resolution Ruleを将来のCrash Transition Matrixで検証する。
- Primary Owner: State Machine Design
- Dependencies: Certificate存在確認、Start Boundary evidence、ExecutionJournal
- Current Fail Closed Default: 次の順序で解決する。(1) Certificate不在または存在確認不能 → Admission Recovery。(2) それ以外でStart Boundary Event未記録 → Execution Startup Recovery。(3) それ以外 → Execution Recovery。
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Resolution Needed Before: Crash Transition Matrixでの検証完了
- Related State: `ST-CLM-002`
- Related Event: `EV-CLM-002`, `EV-CLM-003`
- Source: Base(01), Patch 1(02), Handoff(04) §7

### SM-GEN-002

- Question: (1) Admission atomic commit内のgeneration確定結果不明を、どのPersistent EvidenceまたはAtomic Commit Recovery結果から分類するか。(2) Admission Commit境界外でgenerationを更新する経路が存在するか。(3) 存在する場合のState、Writer、Recovery Owner、Atomicity Boundary。
- Primary Owner: State Machine Design
- Dependencies: Admission atomic commit result、Admission Recovery
- Current Fail Closed Default: Admission-time unresolved commit → Admission Recovery。Admission Commit境界外の経路は非サポートとしてFail Closed。
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Resolution Needed Before: 入力に明記なし
- Related State: `ST-GEN-002`
- Related Event: `EV-GEN-001`
- Source: Base(01), Patch 1(02), Patch 2(03), Handoff(04) §7

### SM-EXE-001

- Question: Fencing Conflict後のLifecycleを定義する: Terminal条件、Recovery Locked経由の要否、ownership再確立後の再開可否、Manual Review条件、claim conflictとownership conflictの区別。
- Primary Owner: State Machine Design
- Dependencies: `SM-DSP-001`(fencing判定に関連する可能性)
- Current Fail Closed Default: 通常実行を停止し、Execution Recoveryが所有する。独断でTerminalへ確定しない。
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Resolution Needed Before: 入力に明記なし
- Related State: `ST-EXE-004`
- Related Event: `EV-EXE-004`
- Source: Base(01), Patch 1(02), Handoff(04) §7

### SM-EXE-002

- Question: Execution Terminalに専用のDurable Eventが必要か、既存Eventの組み合わせから導出するか。
- Primary Owner: State Machine Design
- Dependencies: なし
- Current Fail Closed Default: 専用Eventが未承認の間は、既存Eventからの導出を仮定しない(Open Classification Candidateとして扱う一般原則に従う)
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Resolution Needed Before: 入力に明記なし
- Related State: `ST-EXE-005`
- Related Event: `EV-EXE-003`
- Source: Base(01), Handoff(04) §7

### SM-NOP-001

- Question: No-op Terminal成立に専用のDurable Eventが必要か。
- Primary Owner: State Machine Design
- Dependencies: なし
- Current Fail Closed Default: 専用Eventが未承認の間は、No-op Terminal成立を暗黙に仮定しない
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Resolution Needed Before: 入力に明記なし
- Related State: `ST-NOP-002`
- Related Event: `EV-NOP-002`
- Source: Base(01), Handoff(04) §7

### SM-DSP-001

- Question: Dispatch IndeterminateとOutcome Unknownを、Candidate A(Journal Writerによる明示的durable classification Event)またはCandidate B(承認済みPersistent Evidenceからの一意な導出)のいずれで成立させるか。送信開始後・Confirmed成立前のクラッシュ窓も分類対象に含む。
- Primary Owner: State Machine Design
- Dependencies: `EXE-DSP-003`(Network Invocation Confirmedの境界確定)
- Current Fail Closed Default: 未送信・失敗側へ丸めない。新Permitを発行しない。HTTP送信しない。自動再送しない。Execution Recoveryが所有する。クラッシュだけで即座にDispatch Indeterminateを確立しない。
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Resolution Needed Before: 最終Transition Specification承認前
- Related State: `ST-DSP-006`, `ST-DSP-007`
- Related Event: なし(未採用)
- Source: Base(01), Patch 1(02), Patch 2(03), Handoff(04) §7

---

## Executor Design

### EXE-OWN-001

- Question: ownership generationの割当・handoffを、既存Single Writer境界内でどのように永続化するか。
- Primary Owner: Executor Design
- Dependencies: Single Writer境界(architecture_baseline.md §11)
- Current Fail Closed Default: 永続化方式が未承認の間、ownership generationのPersistent Writerは未確定のまま扱う
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-OWN-001`, `ST-OWN-002`, `ST-OWN-003`
- Related Event: `EV-OWN-001`, `EV-OWN-002`
- Source: Base(01), Patch 1(02), Handoff(04) §7

### EXE-OWN-002

- Question: ownership generationの安全な引き継ぎProtocolは何か。
- Primary Owner: Executor Design
- Dependencies: `EXE-OWN-001`
- Current Fail Closed Default: Protocol未承認の間、handoffを暗黙に安全とみなさない
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-OWN-001`, `ST-OWN-002`, `ST-OWN-003`
- Related Event: `EV-OWN-001`, `EV-OWN-002`
- Source: Base(01), Patch 1(02), Handoff(04) §7

### EXE-DSP-001

- Question: Invocation Claimの再取得条件は何か。
- Primary Owner: Executor Design
- Dependencies: `ST-DSP-002`のsingle-consumer確立
- Current Fail Closed Default: 再取得条件が未承認の間、既存Invocation Claimを保持したまま新規Claimを許可しない
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-DSP-002`
- Related Event: `EV-DSP-002`
- Source: Base(01), Handoff(04) §7

### EXE-DSP-002

- Question: HTTP idempotency keyの設計は何か。
- Primary Owner: Executor Design
- Dependencies: Admission Idempotency Identity(architecture_baseline.md参照)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-DSP-002`
- Related Event: `EV-DSP-002`
- Source: Base(01), Handoff(04) §7

### EXE-DSP-003

- Question: `Network Invocation Confirmed`の具体的・監査可能な成立境界は何か。
- Primary Owner: Executor Design
- Dependencies: `SM-DSP-001`
- Current Fail Closed Default(Patch 2、要件):
  - External Network Invocation開始後にのみ成立する。
  - 承認済みWriterによって耐久的に記録できる。
  - 外部処理完了を主張しない。
  - Dispatch IndeterminateとOutcome Unknownを分離する。
- Current Catalog Blocker: No
- **Blocks Dispatch Transition Specification Final Approval: Yes**
- Resolution Needed Before: Dispatch Transition Specification最終承認前(必須)
- Related State: `ST-DSP-002`, `ST-DSP-003`, `ST-DSP-006`, `ST-DSP-007`
- Related Event: `EV-DSP-002`, `EV-DSP-003`
- Source: Base(01), Patch 1(02), Patch 2(03), Handoff(04) §7

### EXE-ADM-001

- Question: Atomic Prepared Reservationの具体的な実装方式は何か。
- Primary Owner: Executor Design
- Dependencies: Prepared Admission State(architecture_baseline.md §2)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-ADM-001`
- Related Event: `EV-ADM-001`
- Source: Base(01), Handoff(04) §7

### EXE-ADM-002

- Question: Transaction Managerの具体的な実装は何か。
- Primary Owner: Executor Design
- Dependencies: Single Transaction Profile(architecture_baseline.md §1)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-ADM-004`
- Related Event: `EV-ADM-002`
- Source: Base(01), Handoff(04) §7

---

## Recovery Design

### REC-DSP-001

- Question: Dispatch Indeterminateの解消手段は何か。
- Primary Owner: Recovery Design
- Dependencies: `SM-DSP-001`, `EXE-DSP-003`
- Current Fail Closed Default: 入力に明記なし(解消手段が未承認の間、Execution Recoveryが所有し続ける)
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-DSP-006`
- Related Event: なし
- Source: Base(01), Handoff(04) §7

### REC-DSP-002

- Question: 未送信を安全に証明できるPersistent Evidenceは何か。送信開始後・Confirmed成立前のクラッシュ窓を含む。
- Primary Owner: Recovery Design
- Dependencies: `SM-DSP-001`, `EXE-DSP-003`
- Current Fail Closed Default: 未送信は確認できないものとして扱い、再送しない。
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-DSP-002`, `ST-DSP-006`
- Related Event: `EV-DSP-002`, `EV-DSP-003`
- Source: Base(01), Patch 2(03), Handoff(04) §7

### REC-DSP-003

- Question: Invocation Claim保持者の死亡確認はどのように行うか。
- Primary Owner: Recovery Design
- Dependencies: `ST-DSP-002`
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-DSP-002`
- Related Event: `EV-DSP-002`
- Source: Base(01), Handoff(04) §7

### REC-ADM-001

- Question: Prepared StateのTerminal化条件は何か。
- Primary Owner: Recovery Design
- Dependencies: Prepared Admission State
- Current Fail Closed Default: 入力に明記なし(Indeterminate Stateを自動解放しない、というArchitecture制約は維持)
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-ADM-001`, `ST-ADM-002`
- Related Event: `EV-ADM-001`
- Source: Base(01), Handoff(04) §7

### REC-STP-001

- Question: Liveness Policyは何か。
- Primary Owner: Recovery Design
- Dependencies: Execution Startup Recovery
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-STP-002`
- Related Event: `EV-STP-001`
- Source: Base(01), Handoff(04) §7

### REC-STP-002

- Question: Recovery Detectorのscan boundary、watermark、TTLは何か。
- Primary Owner: Recovery Design
- Dependencies: `REC-STP-001`
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-STP-002`
- Related Event: `EV-STP-001`
- Source: Base(01), Handoff(04) §7

---

## Persistent Storage Design

### STO-ADM-001

- Question: PlanSnapshotのPersistent Backendは何か。
- Primary Owner: Persistent Storage Design
- Dependencies: AuthorizedPlan(architecture_baseline.md §4)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: なし(Execution Handoff入力に関連)
- Related Event: なし
- Source: Base(01), Handoff(04) §7

### STO-ADM-002

- Question: Admission Idempotency Identityの物理キー、hash、constraintは何か。
- Primary Owner: Persistent Storage Design
- Dependencies: Admission Idempotency Identity(9要素、architecture_baseline.md参照)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: `ST-ADM-001`
- Related Event: `EV-ADM-001`
- Source: Base(01), Patch 1(02), Handoff(04) §7

---

## Authorization Design

### AUTH-ADM-001

- Question: Reviewer identityとauthority modelは何か。
- Primary Owner: Authorization Design
- Dependencies: Human Review(CLAUDE.md記載の既存原則)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: なし
- Related Event: なし
- Source: Base(01), Handoff(04) §7

### AUTH-ADM-002

- Question: Authorization Scope Boundaryの具体的な粒度は何か。
- Primary Owner: Authorization Design
- Dependencies: authorization scope identity(Execution Handoff照合項目)
- Current Fail Closed Default: 入力に明記なし
- Current Catalog Blocker: No
- Blocks Current Draft: No
- Related State: なし
- Related Event: なし
- Source: Base(01), Handoff(04) §7

---

## Open Question総数

State Machine Design: 8件(`SM-CLM-001`, `SM-GEN-001`を含む)。Executor Design: 7件。Recovery Design: 6件。Persistent Storage Design: 2件。Authorization Design: 2件。合計25件。

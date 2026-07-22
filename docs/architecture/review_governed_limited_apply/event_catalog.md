# Event Catalog — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

この文書は、`Review-Governed Limited Apply`のEvent定義だけを所有する。Architecture Decisionは[architecture_baseline.md](architecture_baseline.md)、State定義は[state_catalog.md](state_catalog.md)、未解決事項は[open_questions.md](open_questions.md)を参照する。

適用順序: Base → Patch 1 → Patch 2。適用判断の詳細は[normalization_ledger.md](normalization_ledger.md)を参照する。

### Event adoption rule(Base由来、変更なし)

Event候補は次のすべてを満たす場合だけ採用する。

- 耐久的に確立された事実を記録する。
- command、request、intent、timeout、absence、observation gapの単なる記録ではない。
- 既存のPersistent Writerが所有できる、または欠落しているWriterが明示的にOpenのままである。
- idempotency identityを記述できる。

### Event Importance分類(Patch 1適用)

| Event set | Importance |
|---|---|
| EV-ADM-001–004 | Mandatory Durable Event |
| EV-CLM-001–004 | Mandatory Durable Event |
| EV-GEN-001 | Mandatory Durable Event |
| EV-OWN-001/002 | Open Classification Candidate |
| EV-STP-001 | Mandatory Durable Event |
| EV-EXE-001/002 | Mandatory Durable Event |
| EV-EXE-003/004 | Optional Derived Candidate |
| EV-DSP-001–005 | Mandatory Durable Event |
| EV-NOP-001 | Mandatory Durable Event |
| EV-NOP-002 | Open Classification Candidate |

Dispatch IndeterminateとOutcome Unknownは、State([state_catalog.md](state_catalog.md) ST-DSP-006/007)のままであり、採用されたEventではない。

---

## A. Admission Events

| Event ID | Candidate Name | Draft Status | Journal / Artifact | Decision Maker | Write Requester | Persistent Writer | Idempotency Identity |
|---|---|---|---|---|---|---|---|
| EV-ADM-001 | Admission Prepared Reservation Committed | Architecture-named | Prepared Admission State | Execution Admission Committer | Same | Admission Artifact Writer | 下記9要素 |
| EV-ADM-002 | Admission Commit Certificate Committed | Architecture-named | Admission Commit Certificate | Execution Admission Committer | Same | Admission Artifact Writer | 下記9要素 |
| EV-ADM-003 | Granted Admission Attempt Result Recorded | Architecture-named | Attempt Result | Committer or Post-commit Audit Repair Coordinator | Same | Admission Artifact Writer | Attempt |
| EV-ADM-004 | Admission Committed Journal Event Recorded | Architecture-named | Admission Journal | Committer or Post-commit Audit Repair Coordinator | Same | Admission Journal Writer | Attempt |

### EV-ADM-001 — Admission Prepared Reservation Committed

Patch 1でIdempotency Identityを拡張。

Idempotency Identity(最低構成、9要素):

- attempt identity
- execution scope identity
- binding identity
- AuthorizedPlan identity
- AuthorizedPlan digest
- environment identity
- database identity
- authorization scope identity
- execution identity

同一Attempt・同一Binding: 既存Reservationまたはcommitted結果を冪等に返す。
同一Attempt・異なるBinding: Conflict。Certificateを生成しない。

Durable Fact: reservationを証明する。Certificateは証明しない。

### EV-ADM-002 — Admission Commit Certificate Committed

Patch 1でTransaction Boundary／Idempotency Identity／Preconditions／Durable Factを更新。

- Transaction Boundary: CertificateとCertificateが証明する全stateは、Single Transaction Profileの単一atomic commitで成立する。commitは、ArchitectureがrequireするActive ClaimおよびCommitted Generation bindingを含む承認済みAdmission factsを対象とする。
- Certificateはpost-commitのAttempt ResultまたはAdmission Journal Eventに依存しない。
- Idempotency Identity: EV-ADM-001と同じ9要素の完全なAdmission identityを使用する。
- Durable Fact: `ST-ADM-004`を証明する。post-commit派生artifactはCertificateを証明しない。
- Forbidden: 重複Certificate作成。

### EV-ADM-003 — Granted Admission Attempt Result Recorded

Base Retained。派生artifactの修復のみを目的とし、Admission evidenceではない。

### EV-ADM-004 — Admission Committed Journal Event Recorded

Base Retained。EV-ADM-003と同様、Admission evidenceではない。

---

## B. Claim Events

| Event ID | Candidate Name | Draft Status | Persistent Writer | Durable Fact |
|---|---|---|---|---|
| EV-CLM-001 | Active Execution Claim Established | Architecture-named | Claim Writer | Claimと初期Fencing Tokenが確立された |
| EV-CLM-002 | Claim Recovery Locked | Architecture-named | Claim Writer | Claimがlockされ、tokenが増加した |
| EV-CLM-003 | Claim Reactivated | Architecture-named / candidate | Claim Writer | 承認されたrecovery reactivationとtoken増加 |
| EV-CLM-004 | Claim Terminal | Architecture-named | Claim Writer | Claimがterminalになり、tokenが増加した |

Base Retained(Event Importance: Mandatory Durable Event、Patch 1適用)。

Forbidden inference: いずれのEventのtokenも、現在のbinding/fencing検証なしに再利用または信頼してはならない。

---

## C. Generation Event

| Event ID | Candidate Name | Draft Status | Persistent Writer | Durable Fact |
|---|---|---|---|---|
| EV-GEN-001 | Execution Scope Generation Committed | Architecture-named | Generation Writer | committed generationがatomicに確立された |

Base Retained(Event Importance: Mandatory Durable Event、Patch 1適用)。

Forbidden inference: generationはClaim Fencing Tokenでもownership generationでもない。

---

## D. Ownership Events

| Event ID | Candidate Name | Draft Status | Event Importance | Persistent Writer | Durable Fact |
|---|---|---|---|---|---|
| EV-OWN-001 | Execution Ownership Assigned | Open | Open Classification Candidate | Open | ownership generationが割り当てられた |
| EV-OWN-002 | Execution Ownership Superseded | Open | Open Classification Candidate | Open | 以前のownership generationが上書きされた |

Patch 1でRECLASSIFY: Mandatory Durable Eventではない。Persistent Writer欠落は、ownership永続化/handoffが明示的に先送りされているため、現在のCatalogをblockしない。永続化とhandoff protocolは未確定。

---

## E. Startup Event

| Event ID | Candidate Name | Draft Status | Persistent Writer | Durable Fact |
|---|---|---|---|---|
| EV-STP-001 | Execution Initialization Event Recorded | Architecture-named | Journal Writer | initializationがcommitされた; Start Boundaryはまだ成立していない |

Base Retained(Event Importance: Mandatory Durable Event、Patch 1適用)。

Forbidden inference: initializationはexecution開始を意味しない。

---

## F. Execution Events

| Event ID | Candidate Name | Draft Status | Persistent Writer | Resulting Evidence |
|---|---|---|---|---|
| EV-EXE-001 | Execution Start Boundary Event Recorded(Operation Path) | Architecture-named | Journal Writer | 下記参照 |
| EV-EXE-002 | Operation Terminal Result Recorded | Architecture-named | Journal Writer | ST-EXE-003 |
| EV-EXE-003 | Execution Terminal Event Recorded | Derived candidate | Journal Writer | ST-EXE-005 |
| EV-EXE-004 | Fencing Conflict Event Recorded | Derived candidate | Journal Writer | 下記参照 |

### EV-EXE-001 — Execution Start Boundary Event Recorded(Operation Path)

Patch 1でResulting Evidenceを更新。

Resulting Evidence: `ST-EXE-001 Execution Started / In Progress`を確立し、ownershipをExecution Recoveryへ不可逆に移管する。

### EV-EXE-002 — Operation Terminal Result Recorded

Base Retained。`ST-EXE-003`を確立する。

### EV-EXE-003 — Execution Terminal Event Recorded

Base Retained(Event Importance: Optional Derived Candidate、Patch 1適用)。`ST-EXE-005`を確立する。Open: `SM-EXE-002`。

### EV-EXE-004 — Fencing Conflict Event Recorded

Patch 1でRecovery Ownership Effectを更新。

- Before: execution終了の起点。
- After: Execution Recoveryへの耐久的な制御移管。Terminalはまだ確立されない。
- Event Importance: Optional Derived Candidate。
- Open: `SM-EXE-001`。

---

## G. Dispatch Events

| Event ID | Candidate Name | Draft Status | Persistent Writer | Preconditions | Resulting Evidence / Durable Fact | Forbidden Inference |
|---|---|---|---|---|---|---|
| EV-DSP-001 | Dispatch Permit Acquired | Architecture-named | Journal Writer | no active Permit for Operation | 下記参照 | 下記参照 |
| EV-DSP-002 | Dispatch Invocation Claimed | Architecture-named | Journal Writer | Permit exists; consumer claim succeeds | 下記参照 | 下記参照 |
| EV-DSP-003 | Network Invocation Confirmed | Architecture-named | Journal Writer | 下記参照 | 下記参照 | 下記参照 |
| EV-DSP-004 | Dispatch Response Recorded | Architecture-named | Journal Writer | Network Invocation Confirmed | ST-DSP-004 | response equals verified terminal result |
| EV-DSP-005 | Dispatch Verification Completed | Architecture-named | Journal Writer | response recorded and verification performed | ST-DSP-005 | verification result may be assumed without durable evidence |

Event Importance: EV-DSP-001–005すべてMandatory Durable Event(Patch 1適用)。

### EV-DSP-001 — Dispatch Permit Acquired

Patch 2でDurable Fact／Forbidden Inferenceを更新。

- Durable Fact: `Operation-level Permit uniquenessが確立された`。
- Forbidden Inference: Invocation consumerが確立された; HTTP送信が許可されている; 送信が発生した。

### EV-DSP-002 — Dispatch Invocation Claimed

Patch 1でPreconditions／Forbidden Inferenceを更新後、Patch 2でDurable Fact／Forbidden Inferenceをさらに更新(Patch 2が最終)。

- Preconditions(Patch 1): Permitが存在し、single consumer claimが成立する。
- Durable Fact(Patch 2、最終): Permitを使用するWrite Executorがsingle consumerとして確立された。
- Forbidden Inference(Patch 2、最終。Patch 1の内容を包含・上書き):
  - すべてのsend guardsが成立した。
  - External Network Invocationが発生した。
  - Claimはsend成功を意味する。
  - 最低条件(送信直前Guard最低条件)がすべて満たされている。

### EV-DSP-003 — Network Invocation Confirmed

Patch 1でPreconditions／Resulting Evidence／Forbidden Inference／Open Questionsを更新後、Patch 2でResulting Evidence／Open Questionsをさらに更新(Resulting EvidenceとOpen QuestionsはPatch 2が最終、PreconditionsとForbidden InferenceはPatch 1のまま維持)。

- Preconditions(Patch 1、維持): 承認済みのpre-send guardsがすべて成立し、External Network Invocationが実行された。
- Resulting Evidence / 証明する事実(Patch 2、最終): `Executor Designで承認されたExternal Network Invocation境界を越えた`。
- 証明しない事実(Patch 2、最終):
  - 外部処理完了
  - Response受信
  - Verification完了
  - Operation Terminal Result成立
- Forbidden Inference(Patch 1、維持): このEventをpre-send guardとして扱う; Eventが耐久化する前に推論する。
- Open: `EXE-DSP-003`(詳細・Blocker構造は[open_questions.md](open_questions.md)を参照)。
- 新しい`External Network Invocation Started` Eventを追加しない(Patch 2で明示的に禁止)。

---

## H. No-op Events

| Event ID | Candidate Name | Draft Status | Persistent Writer | Resulting Evidence |
|---|---|---|---|---|
| EV-NOP-001 | No-op Start Boundary Event Recorded | Architecture-named | Journal Writer | ST-NOP-001; ownershipをExecution Recoveryへ不可逆に移管 |
| EV-NOP-002 | No-op Terminal Event | Derived candidate / Open | Journal Writer(想定) | 専用Eventが承認されればST-NOP-002 |

Event Importance: EV-NOP-001はMandatory Durable Event、EV-NOP-002はOpen Classification Candidate(Patch 1適用)。

Open: `SM-NOP-001`。

---

## I. Architecture Traceability Summary

| Architecture area | Event coverage |
|---|---|
| Prepared Admission State | EV-ADM-001 |
| Admission Commit Certificate | EV-ADM-002 |
| Execution Handoff | EV-CLM-001, EV-STP-001 |
| Recovery Ownership | EV-ADM-003/004, EV-EXE-004 |
| Claim Fencing Token | EV-CLM-002/003/004 |
| Execution Scope Generation | EV-GEN-001 |
| Ownership Generation | EV-OWN-001/002(Open Classification Candidate) |
| Execution Start Boundary | EV-EXE-001, EV-NOP-001 |
| Dispatch Lifecycle | EV-DSP-001–005 |
| Single Writer | 全EventのPersistent Writer欄 |

Trace関係は原則`Enforces`。`EV-DSP-003`は`EXE-DSP-003`について`Blocked By Open Question`(Dispatch Transition Specification最終承認前)。

---

## J. Self-Review(Patch 2適用後、最終)

| Viewpoint | Result |
|---|---|
| Evidence Direction | Pass |
| Dispatch Safety | Pass with Open Questions(`SM-DSP-001`, `EXE-DSP-003`) |
| Single Writer | Pass |
| Fail Closed | Pass |

```text
Architecture blocker: none
State Machine Design draft blocker: none
Catalog result: Pass with Open Questions
```

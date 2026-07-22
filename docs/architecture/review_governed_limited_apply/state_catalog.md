# State Catalog — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

この文書は、`Review-Governed Limited Apply`のState定義だけを所有する。Architecture Decisionは[architecture_baseline.md](architecture_baseline.md)、Event定義は[event_catalog.md](event_catalog.md)、未解決事項は[open_questions.md](open_questions.md)を参照する。

適用順序: Base → Patch 1 → Patch 2 → Handoff制約。適用判断の詳細は[normalization_ledger.md](normalization_ledger.md)を参照する。入力Provenanceは[source_provenance.md](source_provenance.md)を参照する。

Open Questionの詳細全文は複製せず、IDだけを参照する。

---

## A. Classification / Adoption Rules (Base由来、変更なし)

### State split rule

以下のいずれかが異なる場合だけStateを分割する。

- permitted or forbidden behavior
- Recovery Owner
- Persistent Writer
- Reader Visibility
- Active Execution meaning
- required Durable Evidence
- Fencing/generation validity
- Terminal / Non-terminal / Indeterminate classification

### State merge rule

同一Artifact/Aggregate、同一lifecycle meaning、同一Recovery Owner、同一Entry Evidence、同一permitted/forbidden behaviorの場合にmergeする。

### Writer separation

Decision Maker、Write Requester、Persistent Writerは別の役割である。Coordinator、Committer、Recovery Coordinator、Orchestratorは、承認済みWriterとして名指しされない限りPersistent Stateへ直接書き込まない。

---

## B. Admission — ADM

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Reader Visibility | Active Execution Meaning | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|---|---|
| ST-ADM-001 | Admission Prepared | Architecture-named | Prepared Admission State | Durable | Non-terminal | Hidden from normal readers | No | Admission Artifact Writer | Admission Recovery |
| ST-ADM-002 | Admission Commit Indeterminate | Architecture-named | Prepared Admission State | Derived | Indeterminate | Hidden | No | N/A | Admission Recovery |
| ST-ADM-003 | Admission Prepared Conflict Rejected | Proposed | Prepared Admission State | Durable | Terminal | Hidden | No | Admission Artifact Writer | N/A; Admission Recovery for transition crash |
| ST-ADM-004 | Admission Certificate Committed | Architecture-named | Admission Commit Certificate | Durable | Terminal | Visible as committed | No | Admission Artifact Writer | 条件別(下記参照) |
| ST-ADM-005 | Admission Post-commit Artifact Incomplete | Architecture-named | Attempt Result / Admission Journal | Derived | Non-terminal | Certificate visible; derived artifacts incomplete | No | Missing artifact's existing Writer | Post-commit Audit Repair |

### ST-ADM-001 — Admission Prepared

- Entry Evidence: atomic Prepared Reservation succeeds without a TOCTOU gap。
- Exit Evidence: Certificate committed、conflict rejected、またはcommitがindeterminateになる。
- Allowed: 同一Attempt・同一Bindingへ既存reservationを返す。
- Forbidden: 同一Scopeに複数の非Terminal Prepared State; 異なるBindingでの再利用。
- Forbidden interpretation: Preparedは Active Executionを意味しない。
- Recovery Owner: `Admission Recovery`(Patch 1適用。通常進行時の責務とRecovery Ownershipを区別)。
- Architecture Trace: Prepared Admission State(architecture_baseline.md §2)。

### ST-ADM-002 — Admission Commit Indeterminate

- Entry Evidence: Admission Commitが試行され、結果が確認できない。
- Exit Evidence: Admission Recoveryがconflict/rejectionまたはCertificate committedを確立する。
- Allowed: Admission Recovery reconciliationのみ。
- Forbidden: 自動解放; 新規reservation発行; unknownをuncommittedとして扱う。
- Architecture Trace: Admission Recovery(architecture_baseline.md §5)。

### ST-ADM-003 — Admission Prepared Conflict Rejected

- Entry Evidence: conflict rejectionが耐久的に確立される。
- Exit Evidence: なし。
- Forbidden: Certificate作成; 自動再reservation。
- Forbidden interpretation: conflictは自動的にretryableではない。

### ST-ADM-004 — Admission Certificate Committed

- Entry Evidence: CertificateとCertificateが証明するcommitted stateが同一Transactionで成立する。
- Exit Evidence: なし; Execution Handoffの入力として使用される。
- Allowed: Admission evidenceの提供; post-commit派生artifactのトリガー。
- Forbidden: Certificateの書き換え; 後続artifactからのCertificate導出。
- Forbidden interpretation: Certificate単独でhandoffを許可しない。
- Recovery Owner(Patch 1適用、条件別):

  | Condition | Recovery Owner |
  |---|---|
  | CertificateとCommitted Stateが整合し、post-commit派生artifactだけが欠落 | Post-commit Audit Repair |
  | CertificateとCommitted Stateが不整合 | Admission Recovery |

  Post-commit Audit RepairはCertificateまたは証明対象Committed Stateを変更しない。
- Architecture Trace: Admission Commit Certificate(architecture_baseline.md §3)。

### ST-ADM-005 — Admission Post-commit Artifact Incomplete

- Entry Evidence: CertificateとCommitted Stateは整合し、post-commit派生artifact(Attempt Result / Admission Journal)だけが欠落・不整合。
- Exit Evidence: 欠落したAttempt Resultおよび/またはAdmission Journal Eventが既存Writerを通じて修復される。
- Forbidden: Certificateまたはcommitted stateの変更; Recovery Coordinatorによる直接書き込み。

---

## C. Execution Claim — CLM

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-CLM-001 | Active Execution Claim Established | Architecture-named | Active Execution Claim | Durable | Non-terminal | Claim Writer | Context-dependent / Open(`SM-CLM-001`) |
| ST-CLM-002 | Claim Recovery Locked | Architecture-named | Active Execution Claim | Durable | Recovery Locked | Claim Writer | 決定手順あり(下記参照) |
| ST-CLM-003 | Claim Terminal | Architecture-named | Active Execution Claim | Durable | Terminal | Claim Writer | N/A |

### ST-CLM-001 — Active Execution Claim Established

- Entry Evidence: Claim Writerが初期Claim Fencing Tokenを確立する。
- Exit Evidence: Open — Base、Patch 1、Patch 2、Handoffのいずれにも、ST-CLM-001固有のExit Evidenceは明示されていない。ST-CLM-002およびST-CLM-003のEntry Evidenceを相互参照してExit Evidenceを明文化する案はND-001のCandidate Resolutionであり、未承認。現時点では確定済みExit Evidenceとして扱わない。ND-001を参照。
- Monotonic field: Claim Fencing Token。
- Allowed: handoff/fencing validationのための読み取り。
- Forbidden: Claim Writer以外による変更。
- Forbidden interpretation: claimの存在自体が現在の有効性を証明するわけではない。
- Open: `SM-CLM-001`。

### ST-CLM-002 — Claim Recovery Locked

- Entry Evidence: Recovery Locked遷移はatomicであり、Claim Fencing Tokenを増加させる。
- Exit Evidence: 所有するrecovery processがTerminalまたは再活性化されたClaimを確立する。
- Recovery Owner(Patch 1適用、既存の耐久的lifecycle evidenceから次の順序で解決する):

  1. Certificate不在または存在確認不能 → `Admission Recovery`
  2. それ以外でStart Boundary Eventが未記録 → `Execution Startup Recovery`
  3. それ以外でStart Boundary Eventが記録済み → `Execution Recovery`

  使用可能なevidenceはCertificate existence、Execution Scope Generation State、ExecutionJournal、Start Boundary evidenceに限る。新しいPersistent Artifactは導入しない。
- Allowed: 一意に解決されたrecovery ownerによるreconciliation。
- Forbidden: 別のrecovery ownerによるunlock; lock中の通常実行。
- Open: `SM-CLM-002`(詳細は[open_questions.md](open_questions.md)を参照)。

### ST-CLM-003 — Claim Terminal

- Entry Evidence: terminal遷移はatomicであり、Claim Fencing Tokenを増加させる。
- Monotonic field: terminal tokenは再利用できない。
- Forbidden: claim再活性化; fencing token再利用。

---

## D. Execution Scope Generation — GEN

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-GEN-001 | Execution Scope Generation Committed | Architecture-named | Execution Scope Generation State | Durable | Non-terminal | Generation Writer | N/A |
| ST-GEN-002 | Admission-Time Execution Scope Generation Commit Indeterminate | Proposed | Execution Scope Generation State | Open — Durable classification mechanism unresolved | Indeterminate | N/A | Admission Recovery |

### ST-GEN-001 — Execution Scope Generation Committed

- Entry Evidence: Generation Writerが`committed generation`をatomicにcommitする。
- Monotonic field: committed generation; 減少・再利用なし。
- Allowed: handoff比較; Generation Writerを通じた後続generation commit。
- Forbidden: Claim Fencing TokenまたはOwnership Generationとの統合; stale reuse。

### ST-GEN-002 — Admission-Time Execution Scope Generation Commit Indeterminate

Patch 2でCandidate Name／Scope／State Kind／Entry Evidence／Allowed／Forbiddenを再分類。

- Scope: **Admission Commitと同一Atomic Commit境界で行われるExecution Scope Generation確定だけ**。Admission Commit境界外のgeneration更新経路は対象外であり、`SM-GEN-002`としてOpenのまま扱う。
- Recovery Owner: `Admission Recovery`(Admission atomic commit内のgeneration確定に限定)。
- Entry Evidence: Admission atomic commitまたはそのRecovery結果からの承認済みPersistent Evidenceが、generation確定の未解決を一意に確立する。
- Entry Evidenceとして不十分なもの: generation Eventの欠落; timeout; 一時的なReader failure; process absence; memory/cache absence。
- Allowed: Admission Recoveryによるatomic commit結果の検証。
- Forbidden: unknownを旧generationとして扱う; 自動generation retry; Admission owner ruleをAdmission Commit外のgeneration更新へ適用する。
- Open: `SM-GEN-002`(詳細は[open_questions.md](open_questions.md)を参照)。

---

## E. Execution Ownership — OWN

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-OWN-001 | Execution Ownership Assigned | Proposed / Open | Orchestrator execution right(`ownership generation`) | Open | Non-terminal | Open(`EXE-OWN-001`) | Startup Recovery(Start Boundary前) / Execution Recovery(Start Boundary後) |
| ST-OWN-002 | Execution Ownership Superseded | Proposed / Open | 同上 | Open | Terminal for that generation | Open(`EXE-OWN-001`) | Context-dependent |
| ST-OWN-003 | Execution Ownership Handoff Indeterminate | Proposed / Open | 同上 | Derived | Indeterminate | Open | Context-dependent |

本ドメインへのPatch適用なし(Base Retained)。

共通ルール:

- ownership generationは単調増加、後退禁止、再利用禁止。
- Claim Fencing Tokenではない。
- committed generationではない。
- 具体的な永続化・handoff protocolはOpen。
- Open Questions: `EXE-OWN-001`, `EXE-OWN-002`。

---

## F. Execution Startup — STP

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-STP-001 | Execution Initialization Committed | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Startup Recovery |
| ST-STP-002 | Execution Startup Indeterminate | Architecture-named | ExecutionJournal / Active Execution Claim | Derived | Indeterminate | N/A | Execution Startup Recovery |

### ST-STP-001 — Execution Initialization Committed

- Entry Evidence: Execution Initialization Eventが記録される。
- Exit Evidence: operationまたはno-opのStart Boundary Eventが記録される。
- Allowed: Start Boundaryの前提条件を評価する。
- Forbidden: Start Boundary成立前のoperation実行。
- Forbidden interpretation: initializationはexecution開始を意味しない。
- Recovery Owner: `Execution Startup Recovery`(Patch 1適用。理由: Start Boundaryがまだ成立していない)。

### ST-STP-002 — Execution Startup Indeterminate

- Entry Evidence: initialization結果不明、Start Boundary不在、またはstartup-fencing結果不明。
- Allowed: Execution Startup Recovery reconciliationのみ。
- Forbidden: Start Boundaryの先行確立; Execution Recoveryへの早期所有権委譲。

---

## G. Execution — EXE

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-EXE-001 | Execution Started / In Progress | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Recovery |
| ST-EXE-002 | ~~Execution In-Progress~~ | **Deprecated draft alias**(`ST-EXE-001`へmerge済み) | — | — | — | — | — |
| ST-EXE-003 | Operation Terminal Result Committed | Architecture-named | ExecutionJournal | Journal-derived | Terminal per Operation | Journal Writer | Execution Recovery |
| ST-EXE-004 | Execution Fencing Conflict Detected | Open | ExecutionJournal | Journal-derived | Indeterminate | Journal Writer | Execution Recovery |
| ST-EXE-005 | Execution Terminal | Proposed | ExecutionJournal | Journal-derived | Terminal | Journal Writer | N/A |

### ST-EXE-001 — Execution Started / In Progress

Patch 1でMERGE(`ST-EXE-002`を統合)。

- Related Domains: Startup, No-op, Dispatch。
- Entry Evidence: 最初の耐久的なOperation開始Event(Base既定、不変)。
- Exit Evidence: Open — BaseのEXEドメインRulesから、ST-EXE-003、ST-EXE-004、ST-EXE-005との関係候補は識別できる。ただし、現在の入力だけでは、個々のOperation単位のExit Evidenceなのか、Execution全体のExit Evidenceなのかを確定できない。これらのStateとの関係を、確定済みTransitionまたは確定済みExit Evidenceとして扱わない。ND-003、SM-EXE-001、SM-EXE-002を参照。
- Persistent Writer: Journal Writer(不変)。
- Recovery Owner: Execution Recovery(不変)。
- Fencing/generation validity: 不変。
- Allowed: Execution Recovery ownership; Dispatch Permit acquisition; continued Operation execution。
- Forbidden: Execution Startup Recoveryへの復帰; committed-generationまたはownership-generation不一致下でのOperation継続。
- Open Questions: `SM-EXE-001`, `SM-EXE-002`。

### ST-EXE-002 — Deprecated draft alias

- Draft Status: `Deprecated draft alias`。
- Merged into: `ST-EXE-001`。
- Reason: 重複するlifecycle semantics。
- Rule: **このIDを新しい意味で再利用しない**。

### ST-EXE-003 — Operation Terminal Result Committed

Base Retained。耐久的なOperation Terminal Resultを要求する。

### ST-EXE-004 — Execution Fencing Conflict Detected

Patch 1でRECLASSIFY。

- Draft Status: `Open`。
- Lifecycle Kind: `Indeterminate`。
- Recovery Classification: `Recovery Required`。
- Recovery Owner: `Execution Recovery`(不変)。
- Fail Closed default: 通常実行を停止し、Execution Recoveryが所有する。
- Allowed: 耐久的状態の検査; Binding・Claim Fencing Token・committed generation・ownership generationの再評価; 後続のRecovery Designで承認されたactionのみ実行; Terminal・Recovery Locked・制御された再開のいずれが妥当かの判定。
- Forbidden: stale-owner継続; 自動的な通常実行への復帰; 自動再送; ownership変更のみを理由とするPermit再発行; Execution Startup Recoveryへの復帰。
- Related: `EV-EXE-004`のRecovery Ownership Effectは、Execution Recoveryへの移管を意味し、Terminalの証明ではない。
- Open: `SM-EXE-001`(更新済み)。
- **独断でTerminalへ確定してはならない。**

### ST-EXE-005 — Execution Terminal

Base Retained(Proposed)。全Operationがterminal、または承認済みno-op terminal pathを要求する。Open: `SM-EXE-002`。

---

## H. Per-Operation Dispatch — DSP

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-DSP-001 | Dispatch Permit Acquired | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Recovery |
| ST-DSP-002 | Dispatch Invocation Claimed | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Recovery |
| ST-DSP-003 | Network Invocation Confirmed | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Recovery |
| ST-DSP-004 | Dispatch Response Recorded | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Recovery |
| ST-DSP-005 | Dispatch Verification Completed | Architecture-named | ExecutionJournal | Journal-derived | Terminal per Dispatch | Journal Writer | Execution Recovery |
| ST-DSP-006 | Dispatch Indeterminate | Architecture-named | ExecutionJournal | Open — Durable classification mechanism unresolved | Indeterminate | N/A | Execution Recovery |
| ST-DSP-007 | Outcome Unknown | Architecture-named | ExecutionJournal | Open — Durable classification mechanism unresolved | Indeterminate | N/A | Execution Recovery |

Base lifecycle(不変):

```text
Dispatch Permit Acquired
→ Dispatch Invocation Claimed
→ Network Invocation Confirmed
→ Dispatch Response Recorded
→ Dispatch Verification Completed
```

Dispatch Permitは独立Persistent Artifactではない(不変)。

### ST-DSP-001 — Dispatch Permit Acquired

Patch 2でAMEND。

Facts proved:

- Operationに対し有効なPermitは最大1件。
- Permit acquisitionはatomicかつidempotent。
- Operation がDispatch lifecycleへ入った。

Facts not proved:

- send consumerが選択された。
- Invocation Claimが存在する。
- External Network Invocationが許可されている。
- External Network Invocationが発生した。

Single-consumer establishment point: **未確立**(この時点では成立しない)。

Allowed: Invocation Claimの取得を試みる。

Forbidden: Permitのみに基づくExternal Network Invocation; Invocation Claimなしでの Executorによる送信; 追加Permit発行; Permit状態不明時の送信。

Entry Evidence: Open — BaseおよびPatchには、ST-DSP-001固有のEntry Evidenceが独立フィールドとして明示されていない。Permit uniquenessなどのFacts provedを、未承認のままEntry Evidenceへ読み替えない。ND-002を参照。

Exit Evidence: Open — Base lifecycleとEV-DSP-002のPreconditionsからST-DSP-002との関係候補は識別できるが、ST-DSP-001固有のExit Evidenceとしては未承認。ND-002を参照。

Unchanged(Patch 2適用外): Recovery Owner、Persistent Writer。

### ST-DSP-002 — Dispatch Invocation Claimed

Patch 1でAMEND、Patch 2でさらにAMEND(Patch 2の内容が最終)。

Facts proved(Patch 2、最終):

- Permitを使用するWrite Executorがsingle consumerとして確立される。
- そのExecutorだけがpre-send guardsを評価できる。

Facts not proved(Patch 2、最終):

- すべてのpre-send guardsが成立した。
- External Network Invocationが開始した。
- Network Invocation Confirmedが存在する。

Single-consumer establishment point: **`ST-DSP-002`が耐久的に成立した時点**。

Allowed(Patch 2、最終):

- 承認済みのpre-send guardsを評価する。
- すべてのguard成立後、single-consumerであるWrite ExecutorがExternal Network Invocationを開始できる。
- 規範: `1つのInvocation ClaimからExternal Network Invocationを複数回開始してはならない`。

Catalogはat-most-onceの**規範**を定めるだけである。物理的な強制(永続化・locking・I/O制御)はExecutor Design側の作業である。Catalog単独では物理的at-most-once送信を証明しない。

Forbidden(Patch 2、最終):

- pre-send validation完了前の送信。
- 1つのInvocation Claimからの複数回送信開始。
- PermitまたはClaimが不明な状態での送信。
- Operation Binding不一致での送信。
- Claim Fencing Token不一致での送信。
- committed generation不一致での送信。
- ownership generation不一致での送信。
- ClaimがRecovery LockedまたはTerminalの状態での送信。
- Start Boundary成立前の送信。
- stale ownerによる送信。

Forbidden Interpretation(Patch 1、Patch 2で上書きされず維持):

- Invocation Claimedは全guard成立を意味しない。
- Claim取得はsend発生を意味しない。

Exit Evidence(Patch 2、最終):

- Normal: External Network Invocationが開始し、`EXE-DSP-003`で承認される境界を越え、`Network Invocation Confirmed`が耐久化する。
- Unknown: Execution Recoveryが、`SM-DSP-001`で選定される耐久的evidence契約だけを通じてDispatch Indeterminateを分類する。Event不在・timeout・process不在・crashだけでは不十分。crashだけで即座にDurable Stateへ遷移したとは扱わない。

Guard(送信直前Guard最低条件、Patch 1適用): External Network Invocation前に、最低限次を検証する。

- Permit状態が既知かつ有効
- Invocation Claimが既知かつ有効
- Operation Bindingが一致
- AuthorizedPlan identity/digestが一致
- environment identityが一致
- database identityが一致
- authorization scope identityが一致
- Claim Fencing Tokenが一致
- committed generationが一致
- ownership generationが一致
- Active Execution Claimがactiveであり、Recovery LockedでもTerminalでもない
- Execution Start Boundaryが成立している
- 現在のownershipが実行権を持つ

`Network Invocation Confirmed`は送信後のevidenceであり、送信前条件ではない。

Related: `ST-DSP-001`, `ST-DSP-003`, `ST-DSP-006`, `EV-DSP-002`, `EV-DSP-003`, `SM-DSP-001`, `REC-DSP-002`, `EXE-DSP-003`。

### ST-DSP-003 — Network Invocation Confirmed

Base Retained。証明する事実は「承認済みExternal Network Invocation境界を越えた」ことだけであり、外部処理完了・Response受信・Verification完了・Operation Terminal Result成立を証明しない。

Entry Evidence: Open — EV-DSP-003のPreconditionsおよびResulting Evidenceとの対応候補は存在するが、ST-DSP-003固有のEntry Evidenceとしては未承認。ND-002、EXE-DSP-003を参照。

Exit Evidence: Open — Base lifecycleからST-DSP-004との関係候補は存在するが、ST-DSP-003固有のExit Evidenceとしては未承認。ND-002を参照。

### ST-DSP-004 — Dispatch Response Recorded

Base Retained。

Entry Evidence: Open — EV-DSP-004のPreconditionsおよびResulting Evidenceとの対応候補は存在するが、ST-DSP-004固有のEntry Evidenceとしては未承認。ND-002を参照。

Exit Evidence: Open — Base lifecycleからST-DSP-005との関係候補は存在するが、ST-DSP-004固有のExit Evidenceとしては未承認。ND-002を参照。

### ST-DSP-005 — Dispatch Verification Completed

Base Retained。

Entry Evidence: Open — EV-DSP-005のPreconditionsおよびResulting Evidenceとの対応候補は存在するが、ST-DSP-005固有のEntry Evidenceとしては未承認。ND-002を参照。

Exit Evidence: Not applicable under the current catalog classification because ST-DSP-005 is Terminal per Dispatch.

### ST-DSP-006 — Dispatch Indeterminate

Patch 1でRECLASSIFY。

- State Kind: `Open — Durable classification mechanism unresolved`。
- Entry Evidence: (1) 承認済みのdurable classification Event、または(2) 承認済みPersistent Evidenceからの一意な導出。
- Entry Evidenceとして不十分: Event不在; response不在; timeout; process不在; memory flag不在; cache不在; 一時的なReader failure。
- Open: `SM-DSP-001`。

### ST-DSP-007 — Outcome Unknown

Patch 1でRECLASSIFY。

- State Kind: `Open — Durable classification mechanism unresolved`。
- Entry Evidence: Network Invocation Confirmed成立後、承認済みのdurable classification Eventまたは承認済みPersistent Evidenceからの一意な導出を要する。
- Entry Evidenceとして不十分: 不在・timeout・process death・memory/cache不在・一時的read failureのいずれか単独。
- Open: `SM-DSP-001`。

---

## I. No-op Execution — NOP

| State ID | Candidate Name | Draft Status | Aggregate / Artifact | State Kind | Lifecycle Kind | Persistent Writer | Recovery Owner |
|---|---|---|---|---|---|---|---|
| ST-NOP-001 | No-op Start Boundary Established | Architecture-named | ExecutionJournal | Journal-derived | Non-terminal | Journal Writer | Execution Recovery |
| ST-NOP-002 | No-op Terminal | Proposed / Open | ExecutionJournal | Journal-derived or Open | Terminal | Journal Writer | Execution Recovery |

本ドメインへのPatch適用なし(Base Retained)。

- ST-NOP-001 Entry Evidence: Operation件数0に対する耐久的なNo-op Start Boundary Event。
- Recovery ownershipはExecution Recoveryへ不可逆に移管される。
- No-op terminal evidence要件はOpen: `SM-NOP-001`。

---

## J. Architecture Traceability Summary

| Architecture area | State coverage |
|---|---|
| Prepared Admission State | ST-ADM-001/002/003 |
| Admission Commit Certificate | ST-ADM-004 |
| Recovery Ownership | ST-ADM-002/003/005, ST-CLM-002, ST-STP-002, ST-EXE-001/004, ST-DSP-006/007, ST-NOP-001 |
| Claim Fencing Token | ST-CLM-001/002/003 |
| Execution Scope Generation | ST-GEN-001/002 |
| Ownership Generation | ST-OWN-001/002/003 |
| Execution Start Boundary | ST-EXE-001, ST-NOP-001 |
| Dispatch Lifecycle | ST-DSP-001–005 |
| Dispatch Indeterminate / Outcome Unknown | ST-DSP-006, ST-DSP-007 |
| Single Writer | 全StateのPersistent Writer欄 |

Trace関係は原則`Enforces`。Open Question依存箇所は`Blocked By Open Question`として[open_questions.md](open_questions.md)を参照する。

---

## K. Self-Review(Patch 2適用後、最終)

| Viewpoint | Result |
|---|---|
| Reachability | Pass with Open Questions |
| Exclusivity | Pass |
| Monotonicity | Pass |
| Recovery Ownership | Pass |
| Evidence Direction | Pass |
| Dispatch Safety | Pass |
| Single Writer | Pass |
| Fail Closed | Pass |

```text
Architecture blocker: none
State Machine Design draft blocker: none
Catalog result: Pass with Open Questions
```

この結果はDraftの範囲内での自己評価であり、Canonical昇格の可否とは別である。

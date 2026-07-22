# Architecture Baseline — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

この文書は、`Review-Governed Limited Apply`のArchitecture Decisionと不変条件だけを所有する。State定義は[state_catalog.md](state_catalog.md)、Event定義は[event_catalog.md](event_catalog.md)、未解決事項は[open_questions.md](open_questions.md)を参照する。本文書へ全文を複製しない。

入力Provenanceは[source_provenance.md](source_provenance.md)を参照する。本文書は`04-transition-failure-handoff.md`(優先度1入力)を主入力として作成した。

## 承認状態

```text
Architecture blocker: none
State Machine Design draft blocker: none
```

Architecture Decision自体は本文書内で変更・緩和しない。後続のTransition Specification／Typed Failure Catalog設計からも変更・緩和してはならない。

---

## 1. Single Transaction Profile

applyを許可できるのは`Single Transaction Profile`だけである。

以下は、同一Transactional Storeまたは同一Transaction Managerへ参加できなければならない。

- Grant Claim
- Execution Identity
- Active Execution Claim
- Execution Scope Generation State
- Execution Admission Record
- Admission Commit Certificate
- ExecutionJournalの必要なEvent

Multi-store Profileは、具体的なCommit ProtocolとRecovery Protocolが別途Architecture Reviewで承認されるまでapply禁止である。

## 2. Prepared Admission State

Certificate成立前はPrepared Stateである。

必須性質:

- 通常ReaderへCommitted Stateとして公開しない
- Active Executionを意味しない
- 同一Scopeの競合Admissionをブロックする
- Reservation取得は原子的かつ冪等
- TOCTOU窓を作らない
- 同一Scopeの非Terminal Prepared Stateは最大1件
- 同一Attempt・同一Bindingは既存Reservationを返す
- 同一Attempt・異なるBindingはConflict
- Indeterminate Stateを自動解放しない

Writer: `Admission Artifact Writer`

## 3. Admission Commit Certificate

Admission成立の唯一の正本は、`Admission Commit Certificate`である。

参照方向:

```text
Last Durable Pre-commit State
    ↓
Atomic Commit
    ↓
Admission Commit Certificate
    ├─→ Granted Admission Attempt Result
    └─→ Admission Committed Journal Event
```

- Certificateは、Post-commit派生Artifactを参照しない。
- Certificateと証明対象Committed Stateは、同じTransactionで成立する。
- Certificate単独ではExecution Handoffを許可しない。

## 4. Execution Handoff

Admission evidence: `Admission Commit Certificate`

Operation入力: `AuthorizedPlan`

Handoff前に最低限照合する項目:

- Admission Commit Certificate
- AuthorizedPlan identity／digest
- environment identity
- database identity
- authorization scope identity
- Active Execution Claim
- Claim Fencing Token
- committed generation
- Execution Initialization Event
- execution ownership

## 5. Recovery Ownership

### Admission Recovery

- Certificateなし
- Certificate存在確認不能
- Prepared State不明
- CertificateとCommitted Stateの不整合
- Admission Commit不整合

### Post-commit Audit Repair

- Certificateあり
- Committed State整合
- Granted Attempt ResultまたはAdmission Committed Eventだけが欠落・不整合

Post-commit Audit Repairは、Certificateまたは証明対象Committed Stateを変更しない。

### Execution Startup Recovery

- Initialization結果不明
- Start Boundary未成立
- Orchestrator起動状態不明
- Startup Fencing競合結果不明

### Execution Recovery

- Start Boundary成立後
- Operation実行段階以降
- Fencing Conflict
- Dispatch Indeterminate
- Outcome Unknown
- No-op終端失敗

Start Boundary成立後のRecovery Ownershipは不可逆である。Execution Startup Recoveryへ戻してはならない。

## 6. Claim Fencing Token

- Active Execution Claimのstate version
- 単調増加
- 後退禁止
- 再利用禁止
- Recovery Lockedで増加
- Terminalで増加
- Claim Writerだけが更新

## 7. Execution Scope Generation

- committed generationを扱う
- Generation Writerだけが更新
- 単調増加
- 後退禁止
- stale generationを拒否
- Claim Fencing Tokenとは別概念
- ownership generationとは別概念

## 8. Ownership Generation

- Orchestratorの実行権を識別
- 単調増加
- 後退禁止
- 再利用禁止
- Claim Fencing Tokenとは別概念
- committed generationとは別概念

具体的な永続化・引き継ぎProtocolはExecutor Designの未解決事項である(`EXE-OWN-001`, `EXE-OWN-002`)。

## 9. Execution Start Boundary

Operationが1件以上の場合: 最初のDurable Operation開始Event

Operationが0件の場合: No-op Start Boundary Event

Start Boundary成立後は、Execution Recoveryが所有する。

## 10. Dispatch Lifecycle

Dispatch Permitは独立Artifactではない。ExecutionJournal内のEvent系列である。

```text
Dispatch Permit Acquired
    ↓
Dispatch Invocation Claimed
    ↓
Network Invocation Confirmed
    ↓
Dispatch Response Recorded
    ↓
Dispatch Verification Completed
```

正常系の実際の因果順序:

```text
Dispatch Permit Acquired
    ↓
Dispatch Invocation Claimed
    ↓
送信直前検証
    ↓
External Network Invocation
    ↓
Network Invocation Confirmed
```

異常分類:

```text
Permit Acquired／Invocation Claimed以後
かつ
送信有無を安全に確定不能
    ↓
Dispatch Indeterminate
```

```text
Network Invocation Confirmed以後
かつ
Response／Verification／Terminal Resultを確認不能
    ↓
Outcome Unknown
```

### Permit uniquenessとInvocation single-consumerの分離

両者は別の保証である。

- Permit uniqueness: 同一Operationに有効なDispatch Permitは最大1件。成立地点: `ST-DSP-001 Dispatch Permit Acquired`
- Invocation single-consumer: 同一Permitを使用してExternal Network Invocationを実行するWrite Executorは最大1主体。成立地点: `ST-DSP-002 Dispatch Invocation Claimed`

Permit取得だけでは、送信Consumer確定またはHTTP送信許可を意味しない。

### External Network Invocationとat-most-once規範

`ST-DSP-002`では、送信直前Guardの評価と、全Guard成立後・single-consumerであるWrite ExecutorによるExternal Network Invocationの開始を許可する。

State Machine上の規範: 同一Invocation ClaimからExternal Network Invocationを複数回開始してはならない。

具体的なat-most-once強制方式はExecutor Designで定義する。Catalog単独で物理的at-most-once送信を証明したとは扱わない。

### 送信後・Confirmed前のクラッシュ窓

External Network Invocation開始後、`Network Invocation Confirmed`成立前にクラッシュした場合、送信有無を安全に確定できない可能性がある。この場合:

- Confirmed Event不在だけで未送信と判定しない
- Timeoutだけで未送信と判定しない
- クラッシュだけで即時にDurable Stateへ遷移したと扱わない
- 承認済みDurable Evidence契約に従いExecution Recoveryが分類する
- 新Permitを発行しない
- 自動再送しない
- ownership変更を理由に再送しない

分類候補: `Dispatch Indeterminate`

### Network Invocation Confirmed

証明する事実: Executor Designで承認されたExternal Network Invocation境界を越えた。

証明しない事実:

- 外部処理完了
- Response受信
- Verification完了
- Operation Terminal Result成立

具体的成立境界は未確定である(`EXE-DSP-003`)。

## 11. Single Writer

| Persistent State | 唯一のWriter |
|---|---|
| Prepared Admission State | Admission Artifact Writer |
| Admission Record | Admission Artifact Writer |
| Attempt Result | Admission Artifact Writer |
| Admission Commit Certificate | Admission Artifact Writer |
| Admission Journal | Admission Journal Writer |
| ExecutionJournal | Journal Writer |
| Dispatch Lifecycle Event | Journal Writer |
| Execution Scope Generation State | Generation Writer |
| Active Execution Claim | Claim Writer |
| Claim Fencing Token | Claim Writer |

Coordinator、Committer、Recovery Coordinator、OrchestratorはPersistent Stateへ直接書き込まない。

---

## Architecture上の禁止事項

- mainへの直接変更
- 既存Architecture Decisionの変更・緩和
- Architectureにない安全機構の独断追加
- 新しいPersistent Artifact、Writer、Coordinator、Recovery Ownerの独断追加
- Certificate以外をAdmission evidenceにする
- AuthorizedPlan以外をOperation入力にする
- Claim Fencing Token、committed generation、ownership generationの統合
- Start Boundary成立後にExecution Startup Recoveryへ戻す
- Dispatch結果不明時の新Permit発行・自動再送
- Open Questionを成功側の仮定で補完する

## 更新ルール

Architecture Decision自体はArchitecture Reviewの再承認なしに変更しない。Catalog側(state_catalog.md／event_catalog.md)は本文書の該当見出しをArchitecture Traceとして参照し、全文を複製しない。

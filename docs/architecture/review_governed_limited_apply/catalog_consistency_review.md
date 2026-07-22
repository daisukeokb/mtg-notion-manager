# Catalog Consistency Review — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

正規化後のArchitecture([architecture_baseline.md](architecture_baseline.md))、State Catalog([state_catalog.md](state_catalog.md))、Event Catalog([event_catalog.md](event_catalog.md))、Open Questions([open_questions.md](open_questions.md))を横断レビューした結果を記録する。

分類:

```text
Input Conflict
Normalization Defect
Open Question
No Issue
```

意味を変更して問題を修正することはしていない。解決不能な問題はNormalization DefectまたはOpen Questionとして残す。

---

## Catalog件数(Human Review補正済み)

```text
State ID: 29件(ST-EXE-002 Deprecated draft aliasを含む)
Active State count excluding Deprecated alias: 28件
Event ID: 23件
Open Question: 25件
Open Question内訳: 必須23件 + Base由来で保持したSM-CLM-001、SM-GEN-001の2件
```

---

## 検証結果一覧

| # | 検証項目 | 結果 | 分類 |
|---|---|---|---|
| 1 | State ID一意性(全29件、ST-EXE-002 Deprecated draft alias含む) | 重複なし | No Issue |
| 2 | Event ID一意性(全23件) | 重複なし | No Issue |
| 3 | Deprecated ID再利用なし(`ST-EXE-002`) | 新しい意味での再利用なし | No Issue |
| 4 | Entry Evidence Review(全Durable/Journal-derived/Derived State) | `ST-DSP-001`、`ST-DSP-003`、`ST-DSP-004`、`ST-DSP-005`で、State固有のEntry Evidenceが未確定。Event Preconditions／Resulting EvidenceやBase lifecycleとの対応候補は存在するが、未承認のため確定済みEntry Evidenceとして扱わない。Related: ND-002 | Normalization Defect |
| 5 | Exit Evidence Review(全Non-terminal State) | ND-001: `ST-CLM-001`固有のExit Evidenceが未確定。ND-002: `ST-DSP-001`、`ST-DSP-003`、`ST-DSP-004`のState固有のExit Evidenceが未確定(`ST-DSP-005`はTerminal per Dispatchであるため、現在のCatalog分類ではExit Evidenceは適用外)。ND-003: `ST-EXE-001`のExit Evidenceについて、Operation単位とExecution全体単位の粒度が未確定 | Normalization Defect(下記詳細) |
| 6 | State／Event Writer整合 | ADM/CLM/GEN/EXE/DSP/NOPの全ドメインでState・Event間のPersistent Writerが一致 | No Issue |
| 7 | Recovery Owner整合 | Architecture Baseline §5の4区分(Admission Recovery/Post-commit Audit Repair/Execution Startup Recovery/Execution Recovery)の範囲内でState定義と一致 | No Issue |
| 8 | Start Boundary前後 | `ST-EXE-001`/`ST-NOP-001`成立で Execution Startup Recovery→Execution Recoveryへの不可逆移管が一貫 | No Issue |
| 9 | Certificate参照方向 | `EV-ADM-002`/`ST-ADM-004`ともPre-commit→Atomic Commit→Certificate→派生Artifactの方向を維持し、逆参照なし | No Issue |
| 10 | Permitが独立Artifactでない | State Catalog冒頭で明記、ExecutionJournal内Event系列として一貫 | No Issue |
| 11 | Permit uniquenessとInvocation single-consumerの分離 | `ST-DSP-001`(uniqueness)と`ST-DSP-002`(single-consumer)が別Stateとして分離維持 | No Issue |
| 12 | Dispatch IndeterminateとOutcome Unknownの分離 | `ST-DSP-006`/`ST-DSP-007`は別Stateのまま、Entry Evidence契約も個別に維持 | No Issue |
| 13 | Event不在／TimeoutをEvidence化していない | `ST-DSP-006/007`, `ST-GEN-002`のForbidden Entry Evidence一覧で明示的に除外 | No Issue |
| 14 | Claim Fencing Token／committed generation／ownership generationの分離 | Architecture Baseline §6-8で個別定義、`ST-DSP-002`のGuard最低条件でも別項目として列挙 | No Issue |
| 15 | Single Writer | Architecture Baseline §11の表と、全State/Event個票のPersistent Writer欄が一致 | No Issue |
| 16 | 新Artifact／Writer／Recovery Owner追加なし | 使用したWriter・Recovery Ownerはすべて入力に既存のものだけ | No Issue |
| 17 | Open Question欠落なし | 必須25件(State Machine 8、Executor 7、Recovery 6、Storage 2、Authorization 2)すべて登録 | No Issue |
| 18 | Architecture blocker | none(Architecture Baseline、Handoff §2/§7と一致) | No Issue |
| 19 | State Machine Design draft blocker | none(全文書で一致) | No Issue |
| 20 | Input Conflict(Base/Patch1/Patch2/Handoff間の矛盾) | 矛盾する記述は検出されなかった(Patch2はPatch1の一部フィールドをsupersedeするが、矛盾ではなく上書きとして正規に処理) | No Issue |

---

## Normalization Defect詳細

### ND-001: `ST-CLM-001`のExit Evidence未定義

`ST-CLM-001 Active Execution Claim Established`はNon-terminal Stateだが、Base draftの時点でEntry Evidence／Monotonic field／Allowed／Forbidden／Forbidden interpretationは定義されている一方、明示的なExit Evidenceの記載がない。Patch 1／Patch 2ともこのフィールドを対象としていない。

- 影響: `ST-CLM-001`から`ST-CLM-002`(Recovery Locked)または`ST-CLM-003`(Terminal)への遷移条件が、Exit Evidenceとしては明文化されていない(Recovery LockedとTerminalそれぞれのEntry Evidence側からは記述されているため、Fail Closedの実質は保たれている)。
- Status: `Unresolved`
- 対応: `ST-CLM-001`固有のExit EvidenceはOpen／未確定のまま維持する。`ST-CLM-002`および`ST-CLM-003`のEntry Evidenceを相互参照して明文化する案は未承認Candidateである。Canonical昇格前の別作業で、State Machine DesignおよびUser Decisionによる解決方針の承認が必要。Transition Specificationへ先送りしない。
- New Architecture Decision Required: `No`

### ND-002: DSPドメインの一部State(`ST-DSP-001`, `ST-DSP-003`, `ST-DSP-004`, `ST-DSP-005`)がEntry/Exit Evidenceの個票を持たない

ADM/CLM/GEN/STP/EXEの各ドメインはBase draftの時点で個別State見出し(`#### ST-XXX-NNN`)の下にEntry Evidence/Exit Evidence等を持つが、DSPドメインはtable行と、ドメイン全体に対する「Base lifecycle」「Base guarantees」「Base restrictions」の prose だけで構成されており、`ST-DSP-001`, `ST-DSP-003`, `ST-DSP-004`, `ST-DSP-005`個別のEntry/Exit Evidenceが入力のどこにも明記されていない。

- `ST-DSP-001`, `ST-DSP-002`はPatch 2で個別のFacts proved/not proved等が追加されたが、Patch 2自身が`ST-DSP-001`について「Unchanged: Entry Evidence, Exit Evidence」と記載しており、これは**Base側で一度も明示されたことのない値を「不変」と参照している**ことを意味する。
- Status: `Unresolved`
- Classification: `Input Insufficient`
- 対応: `state_catalog.md`では、`ST-DSP-001`、`ST-DSP-003`、`ST-DSP-004`、`ST-DSP-005`のState固有Entry EvidenceをOpen／未確定として明示済みである。`ST-DSP-001`、`ST-DSP-003`、`ST-DSP-004`のState固有Exit EvidenceもOpen／未確定として明示済みである。`ST-DSP-005`のExit Evidenceは、Terminal per DispatchのためNot applicableと明示済みである。Event Preconditions、Resulting Evidence、Base lifecycleとの対応候補を、未承認のままState固有Evidenceへ読み替えない。現在の入力だけでは個別Entry／Exit Evidenceを確定できない。Canonical昇格およびTransition Specification開始前の別作業で、State Machine Designが候補を起草し、Executor DesignおよびRecovery Designを含むCross-Domain ReviewとUser Decisionによる承認を行う。Transition Specification作成中に初めて補完してはいけない。
- New Architecture Decision Required: `No`

### ND-003: `ST-EXE-001`のExit Evidenceがドメインレベルruleからの導出

`ST-EXE-001`のExit Evidenceは、Base draftの個別State見出しではなく、EXEドメイン全体の「Rules」箇条書き(ST-EXE-003/004/005への言及)からの導出である。Patch 1は「Unchanged: Entry Evidence; Exit Evidence」としているが、これも ND-002 と同様に、個別フィールドとして明示的に確定した値への参照ではない。

- Status: `Unresolved`
- Classification: `Open Question Preservation`
- 対応: BaseのEXEドメインRulesから関係候補を識別できるが、Operation単位／Execution全体単位の粒度は未確定である。Candidate Resolution Aは参考候補であり、未承認である。Canonical昇格前の別作業で、State Machine DesignおよびUser Decisionによる解決方針の承認が必要。Transition Specificationへ先送りしない。
- New Architecture Decision Required: `No`

---

## Blocker分類(Human Review補正済み)

| Defect | Blocks Canonical Promotion | Blocks Transition Specification |
| ------ | -------------------------: | -------------------------------: |
| ND-001 | Yes | Yes — Claim関連Transition |
| ND-002 | Yes | Yes — Transition Specification全体 |
| ND-003 | Yes | Yes — Execution関連Transition |

理由: READMEのCanonical昇格条件は、未解決Normalization Defectが0であることを要求する。したがって、ND-001、ND-002、ND-003はすべて、未解決である限りCanonical昇格をBlockする。またCanonical未昇格であるため、現時点ではTransition Specificationを開始できない。

---

## Open Question関連の確認

`SM-CLM-001`と`SM-GEN-001`は、Handoff §7の「現在の主なOpen Question」一覧には掲載されていないが、Base draft Section Eにのみ存在する。Handoffでの非掲載を解消の証拠として扱わず、Openのまま維持した(判断根拠は[normalization_ledger.md](normalization_ledger.md) NL-084/NL-085)。この扱い自体は「Open Questionを成功側の仮定で補完しない」という原則に基づく正規化判断であり、Input Conflictではない。

---

## 総合結果(Human Review補正済み)

`Architecture blocker`、`Draft作成blocker`、`Canonical promotion blocker`、`Transition blocker`は別の軸であり、混同しない。

```text
Architecture blocker: none
State Machine Design draft blocker: none
Input Conflict: 0
Normalization Defect: 3
Catalog Draft review: possible
Canonical Promotion: blocked by ND-001, ND-002, ND-003
Transition Specification: not permitted because Canonical promotion is incomplete
Additional transition impact:
  ND-002 blocks the overall Transition Specification
  ND-001 blocks at least Claim-related transitions
  ND-003 blocks at least Execution-related transitions
Open Question(本レビューで新たに識別): 0件(既存25件から変化なし)
```

Draft内容のレビュー自体は可能である(Architecture blocker/Draft blockerともnone)。ただし、Canonical昇格はND-001/ND-002/ND-003が未解決である限りblockされ、Canonical昇格が未完了である以上Transition Specificationも開始できない。[Blocker分類](#blocker分類human-review補正済み)を参照。

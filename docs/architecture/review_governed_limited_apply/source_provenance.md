# Source Provenance — Review-Governed Limited Apply

```text
Document Status: Draft
Canonical Status: Review Required
```

## 作成目的

`Review-Governed Limited Apply`の承認済み設計入力(Architecture Baseline、State Catalog、Event Catalog、Open Questions、Catalog Consistency Review)を、再利用・レビュー・差分管理可能なRepository内ドキュメントへ正規化するため(作業名: `Canonical Design Input Import`)。

## 入力ファイル(論理名・SHA-256・役割)

| 論理名 | SHA-256 | 役割 |
|---|---|---|
| `01-state-event-catalog-base.txt` | `9808a7b7d1955f1b3ab5d22105b5678fb48f7439f3844c8c413f26ca9a28f25c` | State Catalog／Event Catalog／Open Question／Cross-Catalog Reviewを含む基礎入力(優先度4) |
| `02-state-event-catalog-patch-1.txt` | `471bf499ff54e89c6faa319c2f4c4c78747ab252f020f276fa09d05418fa390e` | 基礎入力へ適用する第1局所修正(7件)(優先度3) |
| `03-state-event-catalog-patch-2.txt` | `bb8b7bc0332154252a926d832a45d36f6daa4c250768d3a5140add3945004916` | Patch 1適用後へ適用する最終局所修正(3件)(優先度2) |
| `04-transition-failure-handoff.md` | `fc53bcbef96df8c2532db8b527fe590716dc1464d42430ddf2a9eaa8cf50bdea` | 承認状態、Architecture Baseline、重要判断、Open Question、禁止事項、次工程を保持する最終制約(優先度1) |

SHA-256は入力ディレクトリに付随していた`SHA256SUMS.txt`と一致することを確認済み(`shasum -a 256 -c`、4件ともOK)。

## 入力優先順位と適用規則

```text
優先度1: 04-transition-failure-handoff.md
優先度2: 03-state-event-catalog-patch-2.txt
優先度3: 02-state-event-catalog-patch-1.txt
優先度4: 01-state-event-catalog-base.txt
```

適用規則: Baseを出発点とし、Patch 1の明示的Target ID／Changed Fieldだけを更新し、次にPatch 2の明示的Target ID／Changed Fieldだけを更新し、最後にHandoffのArchitecture・承認状態・Open Questionを最終制約として適用した。PatchにないBase項目は維持し、Beforeではなく常にAfterを採用し、Deprecated IDは別の意味へ再利用せず、Open Questionは成功側の仮定で補完しなかった。適用判断の詳細は[normalization_ledger.md](normalization_ledger.md)を参照する。

## 基準Git状態

```text
基準branch: main
基準commit(HEAD / main / origin/main): 203f92ef32ace4c341a8445d008a3b24b55cb832
作業branch: docs/review-governed-design-inputs(mainから作成)
```

## 作成日時

2026-07-20(本セッション実行時点)。

## 実施しなかったこと

- 外部通信は行っていない。
- Notionへはアクセスしていない(本作業中)。
- Web検索・GitHubアクセスは行っていない。
- 元入力ファイル(`01`〜`04`)の原文をRepositoryへコピーしていない(本文書および他の正規化文書は要約・再構成された内容であり、原文の複製ではない)。

## 機密情報検査結果

以下のパターンについて、入力4ファイルへローカルでのパターン照合を実施した。

```text
検査パターン: https://app.notion.com/, https://www.notion.so/, notion.so,
NOTION_TOKEN, NOTION_API_KEY, secret_, ntn_, "Bearer ", -----BEGIN,
@gmail.com(および一般的なメールアドレス形式), /Users/, ユーザー名(daisukeokubo),
app.notion, page_id, database_id, 32桁hex連続文字列
```

結果: **該当なし(0件)**。4ファイルはいずれもプレーンテキストのUnicode/UTF-8であり、Notion URL、APIトークン、秘密鍵ヘッダー、メールアドレス、ローカル絶対パス、ユーザー名を含むホームディレクトリ、32桁hex値のいずれも検出されなかった。

入力ファイル内で言及されるGit commit hash(`203f92ef32ace4c341a8445d008a3b24b55cb832`、64桁hex)は、本Repositoryの`main`ブランチの公開済みcommit識別子であり、機密情報として扱わない。

## 構造検証結果

- 4ファイルとも通常ファイル・非空・読み取り可能であることを確認した。
- `01-state-event-catalog-base.txt`にState Catalog／Event Catalogの記載を確認した(該当キーワード90件)。
- `02-state-event-catalog-patch-1.txt`にTarget ID／Before／After形式の記載を確認した(該当キーワード40件)。
- `03-state-event-catalog-patch-2.txt`にTarget ID／After形式の記載を確認した(該当キーワード12件)。
- `04-transition-failure-handoff.md`に承認状態／Architecture／Open Question／blockerの記載を確認した(該当キーワード45件)。

---

## Human Review Correction Pass

このセクションは`Review Provenance`として扱う。上記の入力Source Provenance(元入力4ファイルのSHA-256・優先順位・基準branch・基準commit)とは区別し、いずれも変更していない。本セクションはCanonicalization Review Package v1に対する人間レビュー結果とその補正適用条件だけを記録する。

```text
Review Source: Canonicalization Review Package v1
Review Package v1 SHA-256: a4a2b8ceb0672932313805656c388e6f48eadacef66eb4dc3ce30094b64d8846
Human Review Findings:
  FINDING-001
  FINDING-002
  FINDING-003
  FINDING-004
  FINDING-005
Authorized Repository Document Changes:
  state_catalog.md
  catalog_consistency_review.md
  normalization_ledger.md
  source_provenance.md
Unmodified Repository Documents:
  README.md
  architecture_baseline.md
  event_catalog.md
  open_questions.md
Architecture Meaning Changed: no
State/Event Meaning Newly Decided: no
Normalization Defects Resolved: 0
Normalization Defects Clarified:
  ND-001
  ND-002
  ND-003
External Communication: none
Notion Access: none
Web/GitHub Access: none
Canonical Status: Review Required
```

補正の詳細な監査証跡は[normalization_ledger.md](normalization_ledger.md)の「Human Review Correction Pass(NL-087以降)」を参照する。

---

## Review Package v2 Human Review Correction Pass

このセクションも`Review Provenance`として扱う。上記の入力Source Provenanceおよび先行する「Human Review Correction Pass」セクションは変更していない。本セクションはCanonicalization Review Package v2に対する人間レビュー結果とその補正適用条件だけを記録する。

```text
Review Source: Canonicalization Review Package v2
Review Package v2 SHA-256: 66acb8264979dd4308d55e52ad50f3e58c6574545f1a07f6bad62498c7ebba34
Human Review Findings:
  V2-FINDING-001
  V2-FINDING-002
  V2-FINDING-003
Authorized Repository Document Changes:
  catalog_consistency_review.md
  normalization_ledger.md
  source_provenance.md
Unmodified Repository Documents:
  README.md
  architecture_baseline.md
  state_catalog.md
  event_catalog.md
  open_questions.md
Architecture Meaning Changed: no
State/Event Meaning Newly Decided: no
Entry/Exit Evidence Newly Decided: no
Normalization Defects Resolved: 0
Normalization Defects Clarified:
  ND-001
  ND-002
  ND-003
Resolution Timing Clarified:
  Canonical昇格前の別作業で解決
  Transition Specificationへ先送りしない
External Communication: none
Notion Access: none
Web/GitHub Access: none
Canonical Status: Review Required
```

補正の詳細な監査証跡は[normalization_ledger.md](normalization_ledger.md)の「Review Package v2 Human Review Correction Pass(NL-111以降)」を参照する。

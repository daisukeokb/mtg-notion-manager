# Review-Governed Limited Apply — 設計入力正規化ドキュメント群

## 現在の状態

```text
Document Status: Draft
Canonical Status: Review Required
```

このディレクトリの全文書は、ユーザー承認済みの外部設計入力(`../mtg-notion-manager-design-inputs/`配下の4ファイル)を、Repository内で再利用・レビュー・差分管理可能な形へ正規化したDraftである。ユーザーが内容をレビューし明示承認するまでCanonicalとは扱わない。

Transition SpecificationおよびTyped Failure Catalogの設計は、これらの文書がCanonicalへ昇格した後に開始する。今回のCanonical Design Input Import作業では着手していない。

## Canonical昇格条件(別作業で実施)

1. ユーザーによる明示承認
2. Input Conflictが0
3. 未解決Normalization Defectが0(現時点で3件: ND-001, ND-002, ND-003 — [catalog_consistency_review.md](catalog_consistency_review.md)参照)
4. Catalog Consistency Reviewが完了(実施済み、上記Defectは残存)
5. Architecture、State、Event、Open Question間の参照不整合が0(確認済み)
6. 承認対象のGit diffが固定されている
7. 承認対象commitまたはoperation digestが記録される

## 読む順序

1. [architecture_baseline.md](architecture_baseline.md) — Architecture Decisionと不変条件
2. [state_catalog.md](state_catalog.md) — State定義
3. [event_catalog.md](event_catalog.md) — Event定義
4. [open_questions.md](open_questions.md) — 未解決事項
5. [catalog_consistency_review.md](catalog_consistency_review.md) — 横断整合性レビュー結果
6. [normalization_ledger.md](normalization_ledger.md) — Patch適用・正規化判断の監査証跡
7. [source_provenance.md](source_provenance.md) — 入力ファイルの出所・SHA-256・検査結果

## 各情報の所有文書

| 情報 | 所有文書 |
|---|---|
| Architecture Decisionと不変条件 | `architecture_baseline.md` |
| State定義 | `state_catalog.md` |
| Event定義 | `event_catalog.md` |
| 未解決事項 | `open_questions.md` |
| Catalog間検証結果 | `catalog_consistency_review.md` |
| Patch適用履歴 | `normalization_ledger.md` |
| 入力ファイル・hash・作成条件 | `source_provenance.md` |
| 読む順序・文書境界 | `README.md`(本文書) |

他文書に同じ内容を全文複製しない。参照が必要な場合は所有文書の見出しまたはIDを参照する。

## 恒久ルール

- 時点情報(承認日、レビュースコアの推移、作業セッション情報等)はここへ集約せず、プロジェクトのCurrent State相当の場所で別管理する。本ディレクトリの文書は設計内容の正本(候補)であり、時点情報の正本ではない。
- ArchitectureやCatalogの内容を、後続のTransition Specification／Typed Failure Catalog設計から暗黙に変更しない。変更が必要な場合はArchitecture Reviewまたは追加のCatalog修正を経て、本ディレクトリの文書を明示的に更新する。
- 文書更新時は、同じ変更単位で[normalization_ledger.md](normalization_ledger.md)と[source_provenance.md](source_provenance.md)も更新する。

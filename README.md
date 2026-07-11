# mtg-notion-manager

MTGの統率者(Commander)デッキをNotionで管理するCLIツール。Wizards公式または mtg-jp.com の統率者デッキリストページのURLからデッキ情報を抽出し、Notionの「MTG統率者DB」にレコードを登録する。

## セットアップ

```bash
uv sync  # または pip install -e ".[dev]"
cp .env.example .env
```

`.env` に以下を設定する。

| 変数 | 説明 |
|---|---|
| `NOTION_API_KEY` | Notion Internal Integration のシークレット |
| `NOTION_COMMANDER_DATA_SOURCE_ID` | MTG統率者DBのデータソースID |
| `NOTION_CARD_DATA_SOURCE_ID` | MTGカードDBのデータソースID(現状未使用) |

Notion側で該当ページ/データベースにIntegrationを接続しておくこと([設定 → コネクト] からIntegrationを追加)。

## 使い方

```bash
# Notion認証・DB接続・スキーマ(必須プロパティ/選択肢)の健全性を診断
mtg-notion-manager doctor

# プレビューのみ(Notionへは一切書き込まない)
mtg-notion-manager import <URL> --dry-run

# 内容を確認したうえでNotionへ登録
mtg-notion-manager import <URL>
```

- 抽出結果はNotionへ書き込む前に必ずJSONプレビューを表示する。
- 同名デッキが既に存在する場合は登録せず、差分を表示する。
- 登録時、所有状況は「所有」、タイプは「構築済み」、改造状況は「未改造」で固定。

## 対応サイト

- `magic.wizards.com` の Commander Decklists 記事
- `mtg-jp.com` の統率者デッキ・デッキリスト記事

いずれも **1ページ1デッキの記事のみ対応**。1ページに複数デッキが掲載されている場合(多くの新製品発表記事はこの形式)はエラーとなる。今後の拡張で対応予定。

## 制約・注意事項

- 発売セット名・色名はNotion側の選択肢と完全一致する必要がある。未知の値は `src/mtg_notion_manager/mapping.py` に追記してから再実行すること。マッピングされていない値でNotionに新しい選択肢を自動追加することはしない。
- 外部ページの取得失敗、パース失敗、Notion API失敗時は、不完全なレコードを書き込まずエラー終了する。

## 開発

```bash
pytest
```

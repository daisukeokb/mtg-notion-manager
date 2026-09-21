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

## `--error-json`(構造化エラー出力、pilot: `import-article` / `apply-single-title-update` / `verify-import`)

`import-article`・`apply-single-title-update`・`verify-import` の3コマンドは `--error-json` フラグをサポートする。

- **成功時の出力・終了コードは一切変更しない**(`--error-json` を付けても付けなくても同じ)。
- **失敗時のみ**、人間向けメッセージの代わりに、以下5フィールドだけを持つ1行の純粋なJSONオブジェクトをstdoutへ出力する(Rich装飾・ANSIエスケープ・前後の文言は一切含まない)。

  ```json
  {"schema_version": 1, "command": "import-article", "error_category": "MAPPING", "error_code": "UNMAPPED_VALUE", "message": "..."}
  ```

  - `schema_version`(整数)・`command`・`error_category`・`error_code` は安定した公開契約であり、スクリプトから参照してよい。
  - `message` は人間向けの診断テキストであり**安定した契約ではない**。内容をパースしないこと。
  - `ok` / `details` / `exception_type` / `retryable` / mutation状態を示すフィールドはこのMVPには含まれない。
- **既存の終了コードは変更しない**(`--error-json` の有無で終了コードは変わらない)。
- **本出力は診断情報であり、Notion側の状態変化(mutation)を証明する記録ではない**。特に `error_category: PRODUCTION_API` は、書き込みがNotion側へ実際に到達したかどうかを保証しない。retry安全性の判定にも使用できない。
- **`verify-import` は3-way終了コード(0=検証成功 / 1=登録状態に差分あり / 2=実行エラー)を持つが、`--error-json` が対象とするのは終了コード2(実行エラー)のときだけ**。終了コード1(差分あり)は例外ではなく検証結果であり、`--error-json` を指定していてもJSONは出力しない(既存のdiff出力・終了コード1をそのまま維持する)。
- Typer/Clickのusage error(必須引数欠落など)や、この3コマンド以外のコマンドは対象外(将来のFOLLOW_UP)。

## 開発

```bash
pytest
```

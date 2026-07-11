# CLAUDE.md

MTG統率者デッキをNotionで管理するCLIツール。Wizards公式/mtg-jp.comの統率者デッキ紹介記事をスクレイピングし、Notion「MTG統率者DB」に登録する。

## Notion スキーマ(実測値)

### MTG統率者DB
- データソースID: `39aa97c8-7142-80a1-85c2-000b7f998d48`
- プロパティ: 名前(title) / 統率者(text) / 発売セット(select, 固定選択肢) / 所有状況(select: 所有/購入候補/手放した) / タイプ(select: 構築済み/自作) / 改造状況(select: 未改造/調整中/完成) / 色(multi_select: 白青黒赤緑無色) / テーマ(multi_select) / 強さ(select) / デッキリスト(url) / 採用カード(relation → MTGカードDB)

### MTGカードDB
- データソースID: `81eec501-574b-4222-ad69-87a6f68fdf2b`
- MVPでは未使用(将来のカード連携用に把握のみ)

Notion APIは2025-09-03以降のバージョン(data source対応)を使うこと。ページ作成時のparentは `{"type": "data_source_id", "data_source_id": "..."}`。

## 抽出元サイトの構造(実測、2024年Bloomburrow記事で検証)

### magic.wizards.com
- デッキ名・セットコードは `<deck-list set="BLB" deck-title="Animated Army" format="Commander"><main-deck>...</main-deck></deck-list>` から機械的に取得できる。`set`属性は公式3文字セットコード。
- 統率者は `<main-deck>` の最初の1行(例: `1 Bello, Bard of the Brambles [cardid]`)。末尾の `[...]` は除去する。
- 色は同ページ上部の `<figcaption>Animated Army (Red-Green)</figcaption>` のようにデッキ名と対で書かれている。deck-titleと突き合わせて取得。
- 1記事に複数の `<deck-list>` が存在する場合(新製品発表記事はほぼ全てこの形式)は非対応としてエラーにする。

### mtg-jp.com
- デッキ名は `<h4>「デッキ名」</h4>` (日本語カギ括弧)。
- 色はページ上部の `<strong>「デッキ名」（赤緑）</strong>` から取得。
- 統率者はh4見出し直後、最初に出現する `<strong>...<a class="cardPopupLink" ...>カード名</a>...</strong>` ブロック。
- デッキリスト本体は `<table class="decklist"><caption>「デッキ名」...</caption>...` 。
- セット名は記事タイトル `『ブルームバロウ』統率者デッキ・デッキリスト` の `『』` 内から取得。
- 1記事に複数の `<table class="decklist">` がある場合は非対応としてエラー。

## 重要な設計方針(ユーザー確認済み)

1. **MVPは「1ページ=1デッキ」の記事のみ対応**。複数デッキが検出された場合(`MultipleDecksFoundError`)は処理を中断する。複数デッキ対応は将来の拡張。
2. **発売セット名・色名の未知の値は絶対にエラーで停止する**。`mapping.py` に存在しない値が来てもNotion側に新規選択肢を自動作成しない。マッピング表の手動更新を促す。
3. **不完全なレコードを作らない**。外部ページ取得失敗・パース失敗・Notion API失敗時は、部分的な書き込みをせず全体を中断する。書き込みは1回のcreate呼び出しで完結させる。
4. `--dry-run` は必ずNotion書き込みの直前で分岐し、それより前の処理(取得・パース・マッピング・重複検索)は通常時と同じルートを通す(dry-runでも設計上の問題を発見できるようにするため)。
5. 登録時の固定値: 所有状況=「所有」、タイプ=「構築済み」、改造状況=「未改造」。

## セキュリティ

- `.env` は絶対にコミットしない。APIキー・トークンはコード中にハードコードしない。
- `mapping.py` 中の3文字セットコード表は一部未検証(要確認コメントあり)。誤った値が来た場合は正規化関数がMappingErrorを送出する設計なので、誤ったレコードがNotionに書き込まれることはない。

## テスト

`tests/fixtures/` には実際のページから抽出した単一デッキ分のHTML断片を置く(著作権配慮のため最小限の抜粋)。`pytest` で実行。

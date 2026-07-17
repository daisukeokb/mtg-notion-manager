# CLAUDE.md

MTG統率者デッキをNotionで管理するCLIツール。Wizards公式/mtg-jp.comの統率者デッキ紹介記事をスクレイピングし、Notion「MTG統率者DB」への登録と、「MTGカードDB」とのカード単位のRelation管理・重複統合・価格リンク統合・日本語カード名の安全な更新までを扱う(`src/mtg_notion_manager/cli.py`、2026-07-17時点で全12コマンド実装済み)。

## Notion スキーマ(実測値)

### MTG統率者DB
- データソースID: `39aa97c8-7142-80a1-85c2-000b7f998d48`
- プロパティ: 名前(title) / 統率者(text) / 発売セット(select, 固定選択肢) / 所有状況(select: 所有/購入候補/手放した) / タイプ(select: 構築済み/自作) / 改造状況(select: 未改造/調整中/完成) / 色(multi_select: 白青黒赤緑無色) / テーマ(multi_select) / 強さ(select) / デッキリスト(url) / 採用カード(relation → MTGカードDB)
- `doctor`コマンドが必須チェックする実プロパティは、上記のうち名前/統率者/発売セット/所有状況/タイプ/改造状況/色/デッキリストの8項目(テーマ/強さ/採用カードはPhase3未着手のため必須チェック対象外)。

### MTGカードDB
- データソースID: `81eec501-574b-4222-ad69-87a6f68fdf2b`
- **MVPでは未使用ではなく、実際にはカード単位のインポート・重複統合・価格リンク統合機能で本格的に読み書きしている**(`import-cards` / `dedupe-cards` / `audit-duplicates` / `review-duplicate-conflicts` / `apply-dedupe-plan` / `apply-price-link-dedupe`)。
- 必須プロパティ: カード名(title) / 英語名(rich_text) / 所持(checkbox) / 採用デッキ(relation → MTG統率者DB)
- 任意プロパティ(`dedupe-cards --apply-schema`で追加可能): 所持枚数(number) / 統合済み(checkbox)。メモ(rich_text)は重複統合時の履歴記録に使用。
- 重複統合・価格リンク統合は削除APIを一切使用しない(統合先へのRelation付け替え・メモ追記のみ)。

Notion APIは2025-09-03以降のバージョン(data source対応)を使うこと。ページ作成時のparentは `{"type": "data_source_id", "data_source_id": "..."}`。

## CLIコマンド(実装済み、`cli.py`)

- `doctor`: Notion認証・両DB接続・必須スキーマを診断する。
- `import <url>`: 単一デッキをMTG統率者DBへ登録する(`--dry-run` / `--deck-name`)。
- `import-cards <url>`: 1デッキ分のカード一式をMTGカードDBへ登録・Relationする(`--deck-name`または`--deck-page-id`必須、`--dry-run` / `--apply` / `--allow-count-mismatch` / `--confirmed-card-map`)。
- `import-article <url>`: 記事内の複数デッキのカード一式をまとめて登録する。対象範囲内に曖昧一致・未解決・確認待ちが1件でもあれば全体の書き込みを行わない(all-or-nothing)。
- `verify-import <url>`: 登録済みのはずのデッキ・カード・Relationが実際にNotion上正しいかを読み取り専用で検証する(`--apply`相当のオプションなし)。
- `dedupe-cards`: MTGカードDBの同名重複ページを検出し代表レコードへ統合する(`--dry-run` / `--apply` / `--apply-schema` / `--apply-all`+`--yes`)。
- `audit-duplicates`: 重複グループを読み取り専用で監査しJSON/CSV/Markdownレポートを出力する。
- `review-duplicate-conflicts`: 「要確認」グループをprice-only/special-version/identity-conflict/other/manual-representativeに詳細分類する(読み取り専用)。
- `apply-dedupe-plan`: 監査レポートの「自動統合可能」グループのみ鮮度再チェックのうえ段階適用する。
- `apply-price-link-dedupe`: price-only/manual-representativeグループを段階適用する(`--scope canary/remaining/manual`)。代表ページの販売価格・リンクは上書きしない。
- `plan-title-updates`: 人間確認済みマニフェストの複数件タイトル更新計画を読み取り専用で作成する(dry-run専用)。
- `apply-single-title-update`: 人間確認済み1件だけを、operation digest一致・楽観的ロック・事後検証を経て安全に更新する(対象1件・プロパティ1件・書き込み1回に限定)。

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
6. **重複統合・価格リンク統合は削除APIを使用せず、適用直前に対象を現在のNotion状態で再監査する**(`apply-dedupe-plan` / `apply-price-link-dedupe`)。監査レポート作成時から分類やページ構成が変化していれば、そのグループはスキップする(鮮度チェック)。`apply-price-link-dedupe`はカナリア(3件)→残りの段階適用とし、一括適用しない。
7. **単一カード名更新(`apply-single-title-update`)は対象1件・更新プロパティ1件・HTTP書き込み1回に限定する**。`--apply`時は事前に算出したoperation digestと`--approval-digest`が完全一致しない限り書き込まない。書き込み直前に楽観的ロックで状態変化を検知し、変化があれば中止する。事後検証に失敗しても自動rollbackは行わない。

## セキュリティ

- `.env` は絶対にコミットしない。APIキー・トークンはコード中にハードコードしない。
- `mapping.py` 中の3文字セットコード表は一部未検証(要確認コメントあり)。誤った値が来た場合は正規化関数がMappingErrorを送出する設計なので、誤ったレコードがNotionに書き込まれることはない。

## テスト

`tests/fixtures/` には実際のページから抽出した単一デッキ分のHTML断片を置く(著作権配慮のため最小限の抜粋)。`pytest` で実行。

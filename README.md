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

## `--error-json`(構造化エラー出力、対応: `import` / `import-article` / `apply-single-title-update` / `verify-import` / `doctor` / `audit-duplicates` / `review-duplicate-conflicts` / `plan-title-updates` / `import-cards` / `apply-price-link-dedupe` / `apply-dedupe-plan`)

`import`・`import-article`・`apply-single-title-update`・`verify-import`・`doctor`・`audit-duplicates`・`review-duplicate-conflicts`・`plan-title-updates`・`import-cards`・`apply-price-link-dedupe`・`apply-dedupe-plan` の11コマンドは `--error-json` フラグをサポートする(12コマンド中11)。`dedupe-cards` は未対応(将来のFOLLOW_UP)。

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
- **`import` 固有の安全上の注意**(対応コマンドの中で唯一Notionへ書き込みを行いうるコマンド):
  - `--error-json` は対話確認(「この内容でNotionに登録しますか?」)を一切bypassしない。`--yes` 相当のオプションは存在せず、確認は従来どおり必要。
  - **確認を拒否した場合はError Contract JSONを出力しない**(実行エラーではなく、意図的なキャンセルであるため)。`--dry-run`・重複デッキによるスキップも同様に対象外(いずれも終了コード0・既存の出力のまま)。
  - `import` の書き込みは最大1回(`create_page()`)。このAPI呼び出しは非べき等であり、タイムアウト時・HTTP 500/502/503/504時は自動リトライしない設計になっている(Phase 3F-R1)。**書き込み試行後に発生した実行エラーでは、Notion側にページが作成されていないことの証明にはならない**。自動化スクリプトはこの状態でblind retryしてはならない。再実行前に、対象デッキが既にNotion側に存在していないか(重複状態)を手動で確認すること。
  - **書き込み以前に終了する実行エラー**(設定読み込み失敗・デッキ情報取得失敗・重複確認のNotion読み取り失敗など)は `schema_version: 1`(`mutation`フィールドなし)のまま。**確認後の書き込み(`create_page()`)を試みたうえでの失敗**は `schema_version: 2` となり(Phase 3E-R1)、`error_category`/`error_code` は実際の原因の分類のまま(例: `PRODUCTION_API`/`NOTION_API_ERROR`)、`mutation`(`attempted: 1`)を付与する。サーバーが明示的に拒否したHTTPエラー(400等、リトライを使い切った429)は `MUTATION_FAILED`/`MANUAL_REVIEW_REQUIRED`、HTTP 500/502/503/504・タイムアウト・接続エラーは作成が実際には完了していた可能性を排除できないため `MUTATION_STATE_UNKNOWN`/`RECONCILE_BEFORE_RETRY` になる。いずれの場合も `RETRY_ALLOWED` は出力しない。`mutation` が無い(v1の)エラーは書き込みを一切試みていないことを示す。
  - **`import` の書き込みエラー(確認後・`create_page()`失敗)に限り、stdout全体が純粋な1個のJSONにはならない場合がある**。対話確認の前に人間向けのプレビュー(デッキ名・統率者・色などの内容)が既にstdoutへ表示されているため(確認自体が何を承認しているか人間に見える必要があるため、このプレビュー表示は `--error-json` でも抑制しない)、書き込みエラー発生時はプレビュー出力の後に1行のError Contract JSONが追記される。この1ケースに限り、Error Contract JSONはstdoutの**末尾の1行**として解釈すること(他の7コマンド、および `import` 自身の設定読み込み失敗・デッキ情報取得失敗のような対話確認より前に起きるエラーでは、従来どおりstdout全体が純粋なJSONになる)。
- **`verify-import` は3-way終了コード(0=検証成功 / 1=登録状態に差分あり / 2=実行エラー)を持つが、`--error-json` が対象とするのは終了コード2(実行エラー)のときだけ**。終了コード1(差分あり)は例外ではなく検証結果であり、`--error-json` を指定していてもJSONは出力しない(既存のdiff出力・終了コード1をそのまま維持する)。
- **`doctor` は「doctorコマンド自体が実行できなかった場合(設定読み込み失敗・Notion接続確立の失敗などの実行エラー)」だけが `--error-json` の対象**。個々のチェック項目の合否(診断結果そのもの、たとえばスキーマ不一致の検出)は診断が正常に実行された結果であり実行エラーではないため、`--error-json` を指定していてもJSONへ変換しない(既存の診断テーブル出力・終了コードをそのまま維持する)。
- **`audit-duplicates`・`review-duplicate-conflicts` は監査・分類結果そのものが `--error-json` の対象外**。両コマンドとも、対象グループの分類分布(自動統合可能/要確認/手動指定/除外対象/意図的重複、あるいはprice-only/special-version/identity-conflict/other/manual-representative)がどのような内訳であっても正常終了(終了コード0)であり、実行エラーではない。`--error-json` を指定していても既存の集計出力・レポート生成(JSON/CSV/Markdown)は変更しない。さらに `review-duplicate-conflicts` の不明な `--category` 指定は既存どおりCLIの使い方の誤り(人間向けメッセージ・終了コード1)のままとし、Error Contract JSONへは変換しない。
- **`plan-title-updates` は「1件以上のentryが適用不可(ブロック)」という結果が `--error-json` の対象外**。これはdry-run計画が正常に実行された結果であり実行エラーではないため、`--error-json` を指定していても既存のdry-run出力・終了コード1・レポート生成(JSON/Markdown)を変更しない。対象となるのはマニフェスト不正・件数不一致・Notion読み取りエラーなど、コマンド自体が実行できなかった場合のみ。なお本コマンドはNotionへは一切書き込まない(読み取り専用)が、**ファイルシステムへの書き込みは行う**(成功時に毎回dry-runレポートを`--output-dir`へ出力する)。`--error-json` はこのレポート生成を無効化・変更しない。
- **`import-cards` は、書き込みを1件以上試みた後の結果に応じてschema_versionが変わる最初のコマンド**(Error Contract v2の最初の実接続例。その後 `apply-price-link-dedupe`・`apply-dedupe-plan`・`dedupe-cards`・`import` も同じ方式でv2に接続済み):
  - **書き込み以前に終了する実行エラー**(設定読み込み失敗・`NOTION_CARD_DATA_SOURCE_ID`未設定・`--deck-page-id`/`--deck-name`両方省略・デッキ未検出・曖昧一致による事前ゲートなど)は、他コマンドと同じ `schema_version: 1`(`mutation`フィールドなし)のまま。
  - **1件以上のNotion書き込みを試みたうえでの部分失敗・完了状態不明・中断**は `schema_version: 2` となり、`error_category`/`error_code`/`message` に加えて `mutation` フィールド(`state`/`attempted`/`succeeded`/`failed`/`unknown`/`recovery_action`、失敗・不明分のみの `operations` 配列)を含む。
  - `execute_import_cards()` が正常returnしたうえで1件以上失敗・不明だった場合は `error_category: PARTIAL_MUTATION` / `error_code: CARD_WRITE_PARTIAL_FAILURE`。カード識別確認失敗による中断(`PartialImportAbortedError`)は、`PARTIAL_MUTATION` へは潰さず実際の原因分類(`error_category: IDENTITY_AMBIGUITY` / `error_code: UNVERIFIED_NEW_CARD`)のまま、それに加えて中断までに完了していた書き込みの `mutation` を付与する。
  - `mutation.state` が `MUTATION_STATE_UNKNOWN`(完了状態が不明な書き込みを含む――例: 新規作成のタイムアウトは非べき等でありリトライしない設計のため、サーバー側で実際には成功していた可能性を排除できない)の場合、`recovery_action` は必ず `RECONCILE_BEFORE_RETRY` であり、**盲目的な自動リトライを許可する `RETRY_ALLOWED` にはならない**。既知の失敗のみ(`MUTATION_FAILED`/一部成功のみの`PARTIAL_MUTATION`)の場合も、rerunの安全性が別途証明されるまでは当面 `RETRY_ALLOWED` を出力せず `MANUAL_REVIEW_REQUIRED` に留める。自動化スクリプトは `recovery_action` の値に関わらず、このJSONだけを根拠に自動リトライしてはならない。
  - **`mutation.recovery_action` はNotion mutation stateの解消についてだけを表し、コマンド自体のtop-levelエラー(`error_category`/`error_code`)を解消・代替するものではない**。特に `recovery_action: NONE` は「mutation側の追加対応は不要」を意味するだけであり、「コマンド全体が成功した」ことも「他に何も確認しなくてよい」ことも意味しない。例えば `PartialImportAbortedError`(カードのidentity確認失敗によるbatch中断)で、中断より前の書き込みがすべて成功していた場合、`mutation` は `state: MUTATION_SUCCEEDED` / `recovery_action: NONE` になるが、`error_category: IDENTITY_AMBIGUITY` / `error_code: UNVERIFIED_NEW_CARD` は依然として実際に発生した中断原因を表しており、その解消は別途必要である。自動化スクリプトは常に **`error_category`/`error_code`(コマンドが正常に完了しなかった理由)と `mutation.recovery_action`(Notion書き込み結果について必要な対応)の両方を独立に確認**すること――片方だけを見て判断してはならない。
  - **`--error-json` は `--apply` を意味しない**。`--dry-run`・`--apply`省略時の意味は変更されておらず、正常終了(終了コード0)の出力・内容も変更しない。
  - `--error-json` モード時は、計画表示(サマリ・詳細テーブル)を含むすべての人間向け出力を一時バッファへ書き、成功が確定した場合のみそのままstdoutへ出力する。エラーJSONを出力する場合はバッファの内容を破棄するため、**`import`とは異なり、`import-cards`のエラー出力は常にstdout全体が純粋な1個のJSONオブジェクトになる**(末尾行のみの例外はない)。
- **`apply-price-link-dedupe` は、dedupe-family共有write engine(`dedupe_cards.execute_dedupe_plan()`)の実行結果に接続する最初のdedupeコマンド**:
  - **書き込み以前に終了する実行エラー**(設定読み込み失敗・`NOTION_CARD_DATA_SOURCE_ID`未設定・`--targets-report`の読み込み/パース失敗)は`schema_version: 1`(`mutation`フィールドなし)のまま。
  - **不明な `--scope` の指定・`--scope manual` での代表ページID未指定のようなCLIの使い方の誤りはExecution Errorではないため対象外**(既存の人間向けメッセージ・終了コード1を維持し、Error Contract JSONへは変換しない――`review-duplicate-conflicts`の不明な`--category`指定と同じ境界)。
  - **1件以上のNotion書き込みを試みたうえでの失敗**は`schema_version: 2`となり、`error_category: PARTIAL_MUTATION` / `error_code: DEDUPE_WRITE_PARTIAL_FAILURE`に加えて`mutation`フィールドを含む。`DEDUPE_WRITE_PARTIAL_FAILURE`はdedupe-family共有のcodeであり、`apply-price-link-dedupe`専用ではない(将来`apply-dedupe-plan`/`dedupe-cards`へ`--error-json`を展開する際も同じcodeを再利用する想定)。
  - `mutation.state`が`MUTATION_STATE_UNKNOWN`の場合、`recovery_action`は必ず`RECONCILE_BEFORE_RETRY`(盲目的な自動リトライを許可する`RETRY_ALLOWED`にはならない)。既知の失敗のみの場合も`RETRY_ALLOWED`は出力せず`MANUAL_REVIEW_REQUIRED`に留める――dedupeの書き込みはidempotentな`update_page()`だが、鮮度状態やグループ構成が変化し得るため、計画全体を盲目的に再実行する安全性は未証明である。
  - **カナリア3件のhard cap・鮮度再チェック(stale skip)・実行ログ生成(`write_price_link_apply_log()`)は`--error-json`の有無に関わらず一切変更しない**。特に鮮度不一致によるスキップのみの結果は正常outcomeであり、`--error-json`でもError Contract JSONを出力しない。
  - `--error-json`は`--apply`を意味しない。`--dry-run`・`--apply`省略時の意味・正常終了時の出力・終了コードは変更しない。
  - `--error-json`モード時は、対象一覧表示・適用結果テーブルを含むすべての人間向け出力を一時バッファへ書き、成功が確定した場合のみそのままstdoutへ出力する(`import-cards`と同じ方式――stdout全体が常に純粋な1個のJSONオブジェクトになる)。
- **`apply-dedupe-plan` は、`apply-price-link-dedupe` と同じdedupe-family共有mutation adapter(`dedupe_apply_mutation_adapter.py`、Phase 2Lで両コマンドから使える形へ最小限一般化)を再利用する2つ目のdedupeコマンド**:
  - **書き込み以前に終了する実行エラー**(設定読み込み失敗・`NOTION_CARD_DATA_SOURCE_ID`未設定・`--audit-report`の読み込み/パース失敗)は`schema_version: 1`(`mutation`フィールドなし)のまま。監査レポート読み込み失敗は`error_category: INPUT_VALIDATION` / `error_code: AUDIT_REPORT_LOAD_FAILED`(`apply-price-link-dedupe`の`TARGETS_REPORT_LOAD_FAILED`とは意図的に別code――別の入力artifactのため再利用しない)。
  - **不明な `--classification` 指定のようなCLIの使い方の誤りはExecution Errorではないため対象外**(既存の人間向けメッセージ・終了コード1を維持し、Error Contract JSONへは変換しない)。
  - **1件以上のNotion書き込みを試みたうえでの失敗**は`schema_version: 2`となり、`error_category: PARTIAL_MUTATION` / `error_code: DEDUPE_WRITE_PARTIAL_FAILURE`(`apply-price-link-dedupe`と共通のcode)に加えて`mutation`フィールドを含む。
  - `mutation.state`が`MUTATION_STATE_UNKNOWN`の場合、`recovery_action`は必ず`RECONCILE_BEFORE_RETRY`(盲目的な自動リトライを許可する`RETRY_ALLOWED`にはならない)。既知の失敗のみの場合も`RETRY_ALLOWED`は出力せず`MANUAL_REVIEW_REQUIRED`に留める――dedupeの書き込みはidempotentな`update_page()`だが、鮮度状態やグループ構成が変化し得るため、計画全体を盲目的に再実行する安全性は未証明である。
  - **`--limit`/`--offset` は低リスク順のoperator-controlledなbatchingであり、`apply-price-link-dedupe`の`--scope canary`のようなコード上強制されるcanaryではない**(このコマンドにcanary相当の仕組みは存在しない)。`--error-json`の有無でtarget selectionは変化しない。
  - **鮮度再チェック(stale skip / not-duplicate skip)・実行ログ生成(`write_apply_log()`)は`--error-json`の有無に関わらず一切変更しない**。鮮度不一致・重複解消済みによるスキップのみの結果は正常outcomeであり、`--error-json`でもError Contract JSONを出力しない。
  - `--error-json`は`--apply`を意味しない。`--dry-run`・`--apply`省略時の意味・正常終了時の出力・終了コードは変更しない。
  - `--error-json`モード時は、対象一覧表示・適用結果テーブルを含むすべての人間向け出力を一時バッファへ書き、成功が確定した場合のみそのままstdoutへ出力する(`import-cards`/`apply-price-link-dedupe`と同じ方式――stdout全体が常に純粋な1個のJSONオブジェクトになる)。
- Typer/Clickのusage error(必須引数欠落など)や、この11コマンド以外のコマンドは対象外(将来のFOLLOW_UP)。

## 開発

```bash
pytest
```

### テスト実行時の安全ガード(ネットワーク隔離・認証情報隔離)

リポジトリ直下の `conftest.py` が、リポジトリ内のテストを実行する通常のpytest(`pytest`、`pytest tools/generation_workspace/tests` 等)で、テストのcollectionより前に次のガードを有効にする。ガードを無効にするためのオプション・設定は用意していない。

- **外部ネットワーク通信の遮断**: AF_INET / AF_INET6 のsocketに対する `connect` / `connect_ex` / `sendto` / `sendmsg` / `bind`、`socket.create_connection`、名前解決(`getaddrinfo` 等)を、接続先に関わらず(`localhost`・`::1` を含めて)`NetworkAccessBlockedError` で拒否する。httpx(同期・非同期)・urllib・ssl・asyncioはいずれもこれらを経由する。AF_UNIX(ファイルsocket)は許可する。HTTPを使うテストは `pytest-httpx`(`httpx_mock` fixture)やfakeで差し替えること。
- **認証情報の隔離**: `NOTION_*` 環境変数をテスト用のダミー値で上書きし、python-dotenvの `.env` 自動探索(引数なしの `load_dotenv()` / `dotenv_values()`)を無効化する。リポジトリ直下の `.env` は、パスを明示してもpython-dotenvの `load_dotenv` / `dotenv_values` / `get_key` / `DotEnv` から読めず、`set_key` / `unset_key` から書き換えられない(`CredentialAccessBlockedError`)。判定はパス文字列ではなくファイルの同一性(`os.path.samefile`)で行うため、Unicode正規化・大文字小文字・`..`・リンクの違いでは回避できない。テスト用に明示した別ファイルは読める。
- 各テストの前後でガードが外れていないかを確認し、外れていれば再インストールしたうえでそのテストをエラーにする。
- **限界**: このガードはpytestのプロセス内で、かつこの `conftest.py` が読み込まれる場合にだけ働く。`--noconftest`・リポジトリ直下より下位を指す `--confcutdir`・リポジトリ外のテストファイルでは無効。subprocess・multiprocessing(spawn)の子プロセス、ネイティブ拡張・ctypes、`_socket` を直接使う呼び出しには効かない(子プロセスにはダミーの環境変数だけが引き継がれる)。python-dotenv以外の手段(`open()` 等)による `.env` の読み取り(`open()` したstreamを `dotenv_values(stream=...)` 等へ渡すことを含む)は防がない。実行環境レベルでのネットワーク制限は行っていない。Windowsは未検証。

### 隔離されたテスト実行環境(Layer B、CIの必須チェック)

CIの必須チェック `test` は、リポジトリのコードを実行するすべてのテスト(`pytest`、`pytest tools/generation_workspace/tests`、Generation WorkspaceのCLI smoke)を、ネットワークの無いコンテナの中で実行する。上記のpytestガード(Layer A)はそのまま有効で、Layer Bはその外側で子プロセス・ネイティブコードを含めて通信を止める。

```bash
bash scripts/run_isolated_tests.sh      # 要Docker。Dockerが無ければ失敗する(隔離なしの実行へは切り替えない)
bash scripts/check_isolation_red.sh     # 隔離の各条件を1つずつ外すと失敗することの確認
```

- **準備(ネットワークあり)**: commit済みのHEADだけを空のディレクトリへ取り出し(未追跡の `.env` は含まれない)、そこからimage(`ci/isolated-tests/Dockerfile`、ベースimageはdigest固定)をbuildして依存関係をinstallする。build contextは `.dockerignore` でも `.env` を除外する。CIのcheckoutは `persist-credentials: false`。
- **実行(ネットワークなし)**: `docker run --network none`(loopbackのみ)、`--read-only`(書き込みは `/tmp` と `/work` のtmpfsのみ。`/app` は読み取り専用の元ソースで、自己検査の後に `/work` へコピーしてそこでテストを実行する。テストの一部が作業ツリー内の `reports/` 等へ書き込むため)、`--cap-drop ALL`、`no-new-privileges`、非rootユーザー(uid 10001)、volume mountなし、Docker socketなし。環境変数はホストから引き継がず、`-e` で明示したダミーの `NOTION_*` だけを渡す。
- **自己検査(fail-closed)**: テストの前に `ci/isolated-tests/verify_isolation.py` が、loopback以外のinterface・routeが無いこと(無ければプローブを送る前に停止)、Layer Aを通らない `_socket`・子プロセス・ネイティブ(git/libcurl)の接続が即座に「到達不能」で失敗すること、名前解決が失敗すること、ダミー以外の認証情報・秘密らしい環境変数・`.env`・Docker socketが無いこと、非rootでアプリケーションディレクトリが読み取り専用であることを確認し、1つでも満たさなければテストを実行しない。
- **限界**: commit済みの変更だけが対象(未commitの変更はLayer Bでは検証されない)。`--network none` はloopbackを残すため、コンテナ内のlocalhost通信まではOSレベルで遮断しない(Layer Aがpytestプロセス内で遮断する)。Python依存関係は `requirements/ci-lock.txt`(hash付き)に固定済み(下記)。ローカルのmacOSでDockerが無い場合はLayer Bを実行できない(正式な検証はCI)。`lint`・`type-check` と Generation Workspaceのruff/mypyは、コードを実行しない静的検査のため隔離外で実行する。

### CIの供給網・認証情報の固定(Phase 3H-R2)

- **GitHub Actions**: `uses:` はすべて完全な40文字のcommit SHAで固定し、行末にreleaseタグをコメントで残す(`actions/checkout` v7.0.1、`actions/setup-python` v7.1.0。Phase 3H-R4でNode.js 24で動く版へ更新)。許可するActionとそのcommit SHAは `ci/static_guard.py` の `ALLOWED_ACTIONS` に限る(Phase 3H-R3で、レビュー済みSHA以外の40文字SHA=fork由来のcommit等も拒否するよう強化)。
- **認証情報**: すべての `actions/checkout` で `persist-credentials: false`(`.git/config` にtokenを残さない)。各jobはcheckout直後に `python3 -I scripts/check_no_persisted_credentials.py` で認証情報が残っていないことを確認する(Phase 3H-R3で下記のとおり作り直した)。workflowの権限は `contents: read` のみで、`secrets.` は使わない。
- **ベースimage**: `python:3.14.10-slim-trixie`(Debian 13)のlinux/amd64 image manifest digestで固定し、`docker build --platform linux/amd64` でbuildする。Dockerがpull時にdigestで内容を検証するため、tagの付け替えでは差し替わらない。CIのrunner側のPython(`setup-python`)もimageと同じ3.14.10に固定する(Phase 3H-R4でサポート終了済みの3.9.25・Debian 12から更新)。
- **対応Pythonの範囲**: CIで実行・テストするのはCPython 3.14.10だけで、mypyもCIの依存関係(Python 3.10以上を要するclick 8.5等)と同じ `python_version = "3.14"` で検査する。`pyproject.toml` の `requires-python = ">=3.9"` とruffの `target-version = "py39"` は、手元の開発環境(CPython 3.9)でもインストール・構文解析できるよう残している下限であり、3.9での実行・型検査はCIで検証しない(3.9はサポート終了済みのため、手元の環境の更新は別作業とする)。
- **Python依存関係**: `requirements/ci-lock.txt` に、両プロジェクトの実行時依存・`dev` 依存・buildバックエンド(hatchling、editables)を推移的依存まで `==` で固定し、各versionの公開ファイルすべてのsha256を記録する。CIとimageは `pip install --require-hashes --no-deps --only-binary=:all: -r requirements/ci-lock.txt` で入れ、ローカルの2プロジェクトは `--no-deps --no-build-isolation --no-index` で(取得なしで)入れた後、`pip check` で整合性を確認する。`--only-binary=:all:` によりsdistのbuild(hash検証されないbuild依存の取得)は起こらない。runner側のpipについてはPhase 3H-R3の節を参照。`pyproject.toml` との整合(全宣言が含まれ、範囲指定を満たすこと)はテストで検査する。開発用の `pip install -e ".[dev]"` はこれまでどおり使える(lockはCIとLayer B imageの再現性のため)。

### CI検査器の作り直し(Phase 3H-R3)

- **認証情報の検査(`scripts/check_no_persisted_credentials.py`)**: 個々の設定ファイルをgrepするのではなく、Gitが実際に使う設定を読む。
  - `git config --list --show-scope --show-origin --includes -z` で、Gitが読むすべてのscope(system・global・local・worktree・組み込み既定・`GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_COUNT` の `command`)を、include先まで含めて取得する。
  - `git remote get-url --all` と `--push --all` で、`url.*.insteadOf`/`pushInsteadOf` による書き換え後の実効URLを確認する。`git remote` に表示されない旧形式のremote定義(`$GIT_DIR/remotes/`・`$GIT_DIR/branches/`)は存在自体を不可とする。
  - 環境変数: `GIT_ASKPASS`/`SSH_ASKPASS`、`GIT_SSH`/`GIT_SSH_COMMAND`、`GIT_SSL_KEY`/`GIT_SSL_CERT`/`GIT_PROXY_SSL_KEY`/`GIT_PROXY_SSL_CERT`、`SSH_AUTH_SOCK`、ユーザー情報付きのproxy変数(`https_proxy` 等)を検出する。`GIT_CONFIG`(`git config` だけが別ファイルを読む)と `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE`(checkoutとは別のrepositoryを検査させる)は、設定されていること自体を不可とする。home directoryの認証情報ファイル(`~/.netrc`/`~/_netrc`、credential storeの `~/.git-credentials`/`$XDG_CONFIG_HOME/git/credentials`、ssh秘密鍵 `~/.ssh/id_*`)は存在だけを確認し、開かない。
  - 検出対象: `credential.*`、`http.*.extraheader`/`cookiefile`/`sslkey`/`sslcert`/`proxysslkey`/`proxysslcert`、`core.askpass`、`core.sshCommand`、パスワード設定(`sendemail.*.smtpPass`、`imap.pass`)、ユーザー情報付きのproxy設定(scheme省略形を含む)、include(`include.path`/`includeIf.*.path`。include先と条件はレビュー対象外のため、存在自体を不可とする)、設定のキー名・値のどこかにある認証情報付きURL(http/https/ftp/ftpsはユーザー情報の有無、それ以外はパスワードの有無)。`url.https://github.com/.insteadOf git@github.com:` のような安全な書き換えは通す。
  - 表示はscopeとキー名(subsectionは伏せ字)だけで、値は表示しない。Gitを実行できない、非0で終了する、stderrに何か出力する、出力を解析できない場合は、検出なし扱いにせず失敗する。終了コードは 0=認証情報なし、1=検出、2=検査を完了できない。
  - CIのrunnerでは、各jobの冒頭(checkout直後)に実行し、検査対象はそのjob自身のGit環境に限る。テストは一時repositoryと一時のglobal設定だけを使い、system設定は無効化する(`GIT_CONFIG_NOSYSTEM=1`。macOSのApple Gitは組み込みの `credential.helper=osxkeychain` を持つため、これを外さないと正しく検出されて失敗する)。
- **静的検査(`ci/static_guard.py`)**: workflow・Dockerfile・Layer Bのrunner・`scripts/*.sh` を検査する。基本は「レビュー済みの形だけを通し、それ以外は違反」(deny by default)。
  - workflow: 印字可能なASCIIだけを受け付ける(YAML 1.1のパーサーが改行として扱うNEL・U+2028・U+2029などで、検査器に見えないキーを隠せないようにするため)。YAMLは、レビュー済みの狭い正準形(ブロック形式のmapping/sequence、plain・二重引用符・`|` のscalar)だけを受け付け、anchor/alias・tag・flow形式・引用符付きキー・重複キー・タブ・複数document・エスケープなどは、それだけで違反とする。job・キー・トリガーの集合、`permissions: contents: read`、各jobの最初のstepがcheckoutであること、`persist-credentials` がplainの `false` であること、checkout直後の認証情報検査、setup-python直後のpip bootstrap、`secrets`/式の不使用を検査する。`run` の各行は `REVIEWED_RUN_LINES` のいずれかと完全一致しなければならず(`bash -c`・`eval`・`$GITHUB_ENV` への書き込みなども、それだけで違反)、各jobのstepの並びも `EXPECTED_STEPS` と完全一致しなければならない(Layer Bのテストやred checkのstepを外すと違反)。トリガー・`concurrency`・setup-pythonのcache設定もレビュー済みの値に固定する。
  - 次のファイル(`REVIEWED_FILES`)は、レビュー済みの内容(sha256)と一致しなければならない: runner上で実行されるscript(`scripts/run_isolated_tests.sh`・`check_isolation_red.sh`・`bootstrap_locked_pip.sh`・`check_no_persisted_credentials.py`)、runner上のpip・hatchling・mypyがコードを読み込む先を指定しうる設定(2つの `pyproject.toml`。build hook・build backend・mypy pluginはここに書かれる)、Layer A(`conftest.py`)とLayer Bの定義(`ci/isolated-tests/` のDockerfile・`run_in_container.sh`・`verify_isolation.py`、`.dockerignore`)。`scripts/*.sh` と `ci/isolated-tests/` のファイル集合も固定し、レビュー対象外のbuild・型検査設定(`setup.py`・`setup.cfg`・`hatch_build.py`・`hatch.toml`・`mypy.ini`・`.mypy.ini`)は存在自体を不可とする。また、runner上のPython process(pip、そのbuild backend subprocess、mypy)の `sys.path` にはrepository内の次のdirectoryが入る: `python -m` の作業directoryであるroot(Python 3.9には `-P` がない)と、editable installの `.pth` が追加する `src/`・`tools/generation_workspace/`。ここにレビュー済み以外のimport可能なもの(`.py`・`.pth`・拡張module(ABI tag付きの `__init__.*.so` を含む)・package・moduleを含むnamespace directory・名前がimport可能なsymlink)を置くと、`sitecustomize` や任意依存(`orjson` 等)としてpip・mypyに読み込まれうるため、存在自体を不可とする(`IMPORT_ROOTS`。許可はrootの `conftest.py`・`tests`・`ci`・`scripts`・`src`・`tools`、`src/` の `mtg_notion_manager`、`tools/generation_workspace/` の `generation_workspace`・`tests`)。これらのdirectoryは実在するdirectoryでなければならず、欠落や、途中のsymlink(guardを実行する環境とrunnerとで解決先が変わりうる)は違反とする。許可したpackageの中身はsha256で固定しないが、runner上のjobはそれを解析(mypy・ruff)するだけでimportしない。CIではguardはLayer Bのcontainer内で実行され、そのimageは `.dockerignore` のpathを除いてbuildされる(runnerのcheckoutには残る)ため、これらのpathの下にtrackedなfileを置くことを不可とする(`git ls-files` で列挙。gitが失敗した場合も違反。`TRACKED_FILE_EXCLUDED`)。これにより、container内のguardもrunnerのcheckoutと同じtracked treeを検査する(runner上でjobが生成するのは `.mypy_cache/` 等のimportできない名前のcacheのみ)。shellは静的解析で完全には検証できないため、これらのファイルについてはこの固定が判定の根拠になる。
  - そのうえで、理由を特定できる二次的な検査として、Dockerfileは単一のdigest固定FROM、parser directive(`# syntax=` 等)・instruction flag(`--from`・`--mount` 等)・exec形式のRUN・ADD/ARGの不使用、レビュー済みのENV名、最後のRUNより後の `USER 10001:10001` を、runnerは `docker run` が1回だけで隔離optionが完全一致すること、`docker info` とレビュー済みoptionの `docker build` 以外のdocker操作がないことを検査する。pipは、引数を分解してから、hash固定の形またはオフライン・ローカルの形(`-r`/`-e` は `..` を含まないリテラルのローカルパスのみ。pipの取得元を変える `PIP_*` 環境変数の設定も不可)で、かつレビュー済みのoptionだけを使う場合に限って許可し、`eval`・`sh -c`・stdinから読むshell・`python -c`/`python -`・repository外のscript・`env -S` などprefixのoption・perl/awk等の他の言語処理系・展開で組み立てたコマンド名などは違反とする。subshell・コマンド置換・trap・`case` の各分岐の中身も同じ規則で検査する。ただし、shell解析は網羅的ではない(例: `xargs`・`find -exec`・`sed` の `e` 命令経由の実行は解析しない)ため、上記の内容固定を外して解析だけに頼ることはしない。
  - 意図して構成を変えるときは、同じ変更でguard(`REVIEWED_RUN_LINES`・`REVIEWED_FILES` のsha256・`ALLOWED_ACTIONS`・`BASE_IMAGE` など)も更新する。sha256の不一致は違反メッセージに新しい値が表示されるが、値を更新する前に差分そのものをレビューすること。
  - 限界: guardはPR自身のtreeでテストとして実行されるため、PRはguardと構成を同時に変更できる。guardの役割は、セキュリティ上の変更をレビューで見落とせない形(guard自体の差分)にすることであり、強制の境界はbranch保護とレビューである。また、pull_requestのCIはPRのworkflow・コードをネットワークのあるrunner上で実行する(secretsなし、`contents: read`、token非保持)。lint・type-check・generation-workspaceのjobは、リポジトリのコードをrunner上で扱う(editable install時のhatchlingによるbuild、mypy・ruffによる解析)。PRがguard自体を変更しない限り、上記の内容固定とimport可能な名前の制限により、そこからrepository内のコードが実行される既知の経路は塞いでいるが、runner上でrepositoryのコードを扱うこと自体は残る。これを無くすには、静的検査もLayer Bのcontainer内で実行するjob構成の変更が必要(今回は未実施)。
- **runnerのpip(`scripts/bootstrap_locked_pip.sh`、`requirements/ci-pip-lock.txt`)**: setup-pythonの導入処理がlock外で更新したpipを、依存関係を入れる前に、hash固定のpip(26.0.1のwheel)に入れ替える。wheelはHTTPSだけで取得し、そのコードを実行する前にcoreutilsの `sha256sum` で検証する。そのうえで、検証済みwheelの中のpipでオフライン(`--no-index --require-hashes`)に入れ直すため、検証されていないpipは以後実行されない。
  - 残る境界: setup-pythonがversion指定だけで取得するPython本体と、その導入処理が一度だけ実行した(lock外の)pip。どちらもhash検証されない。lock外のpipが実行時にRECORD外へ残したファイル(`.pth` 等)は `--force-reinstall` では消えず、site-packagesの `.pth` は `python -I` でも読み込まれる。lint・type-check・generation-workspaceのjobだけに関係し、これらはsecretsを持たず、tokenも残さない。解消するには、静的検査もdigest固定のimage内で実行するなど、job構成の変更が必要。
- **負のテスト**: `tests/test_credential_check.py` と `tests/test_ci_static_guard.py` は、ダミー値で危険な設定を作り、期待した理由(終了コード、scope・キー、違反コード)で失敗することを確認する。テスト自身のクラッシュや変異の適用失敗は、成功として数えない。

#### 依存関係・Action・ベースイメージの更新手順

1. **Python依存関係**: `python -I scripts/lock_dependencies.py` を実行する(PyPIへの通信が必要。認証情報は読まず、リポジトリのコードはimport・buildしない)。CPython 3.14 / manylinux x86_64向けに解決し、PyPIの公開hashと照合して `requirements/ci-lock.txt` を書き換える。pipは環境マーカー(`python_version < "3.11"` 等)を実行中のPythonで評価するため、解決結果をCIのPython(`TARGET_PYTHON`)のマーカーで評価し直し、不要な配布物は除き、CIのPythonだけが必要とする依存(や版)が足りなければ、それを明示して解決し直す。既存のlockのversionは制約として維持し(差分を最小にする)、最新へ上げる場合は `--upgrade` を付ける。差分(version・hashの変化)をレビューしてcommitする。runnerのpipだけを更新する場合は、`PIP_VERSION` を変えて `--pip-only` を付けて実行する(`requirements/ci-pip-lock.txt` だけを書き換える)。
2. **Action**: 公式repositoryのrelease/tagを確認し、`gh api repos/<owner>/<repo>/git/ref/tags/<tag>` で得たcommit SHA(annotated tagの場合はcommitまでたどる)と `# <tag>` コメントを、workflowと `ci/static_guard.py` の `ALLOWED_ACTIONS` の両方で更新する。
3. **ベースimage**: registryからtagのOCI image indexを取得してlinux/amd64のmanifest digestを確認し、Dockerfile冒頭のimage・Python version・OS・digest・index digest・確認日を更新する(runner側の `python-version`、`scripts/lock_dependencies.py` の `TARGET_PYTHON`、`ci/static_guard.py` の `BASE_IMAGE`、Dockerfileの内容固定(`REVIEWED_FILES`)も合わせる。これらの一致はテストで検査する)。digestの固定は内容の同一性を保証するだけで、含まれるパッケージの脆弱性を保証しないため、更新時(および定期的に)ベースimageのセキュリティ更新を確認する。
4. いずれもCIの `test`(Layer B自己検査・red check 8件を含む)・`lint`・`type-check`・`generation-workspace` がすべて成功することを確認してからmergeする。

# Phase 3H-R7 Production Cutover Runbook

**この手順書は本番切替の承認ではない。PRODUCTION CUTOVER NOT AUTHORIZED.** R7では本番切替を実施しておらず、手順の検証は合成設定と隔離した模擬環境(`tests/test_cutover_readiness_*.py`)だけで行った。

切替の対象は、ローカルのCLI実行環境を `.venv`(CPython 3.9.6)から `.venv-py314-r5`(CPython 3.14.10)へ移すこと。既存の手順(`docs/local-python-runtime.md`「切り替え手順」「切り替え前の安全ゲート」)を土台とし、R7で追加した検査(`scripts/cutover_readiness.py`)と、C-2/C-3・外部状態の扱いを加える。手順の途中で1つでも期待と違えば、その場で止める(fail closed)。自動の修復・再インストールはしない。

## 1. Pre-cutover evidence review

- 確認するもの: R6の証跡(`docs/evidence/phase-3h-r6/`)、R7の証跡(`docs/evidence/phase-3h-r7/`)、`R7_RESIDUAL_RISK_REGISTER.md`、`R7_GITHUB_GOVERNANCE_AUDIT.md`。
- 合格条件: 各証跡のsha256が記録と一致し、検証対象commitがmainに到達していること。
- 止める条件: 証跡が無い・hashが違う・未解消のCutover Blockerがある。

## 2. Production approval verification

- 切替には、本手順とは別の、明示的な人間の承認記録(Production Cutover Authorization)が必要。R7のHuman Acceptance、PRのmerge、CIのPASSはいずれも切替の承認ではない。
- 承認記録に含めるもの: 承認者、日時、対象commit、対象runtime(archive sha256)、受け入れた残リスク(ID)、Blocker B-G1・B-D1・B-C23・B-RB の扱い。
- 止める条件: 承認記録が無い、または対象commit・runtimeが一致しない。

## 3. Configuration inventory

- 本番の設定項目は `NOTION_API_KEY`・`NOTION_COMMANDER_DATA_SOURCE_ID`(必須)、`NOTION_CARD_DATA_SOURCE_ID`(任意)の3つだけ(`src/mtg_notion_manager/config.py` が読む値。testで一致を確認)。
- 現状の読み込み経路: 引数なしの `load_dotenv()` がリポジトリ直下の `.env` を自動で探し、shellの `NOTION_*` があればそちらが優先される(C-2)。
- 切替後の読み込み経路(案): launcherの設定file 1つだけ(下記6)。`.env` の自動探索は `PYTHON_DOTENV_DISABLED=1` で無効化する。
- 棚卸しでは**値を表示・複製しない**。記録するのは項目名、fileの所有者・mode、sha256(fingerprint、rollbackの比較用)だけ。
- 止める条件: 想定外の項目がある、設定fileが他人に読める、shellに `NOTION_*`・`PYTHON*`・`DYLD_*`・`LD_*`・`PIP_*` がある。

## 4. Trusted runtime confirmation

1. `docs/local-python-runtime.md` のCutover Safety Gate(`scripts/verify_cutover_gate.py`、Step 0と項目1〜8)を最初から実行し、全項目PASSであること。以前の実行結果は再利用しない。
2. Runtime manifest(`scripts/cutover_readiness.py` の `MANIFEST_SCHEMA`)を用意し、**そのsha256を、manifestとは別の場所(承認記録)に人間が記録する**。manifestを作った同じ環境で、同時にhashを記録しただけでは独立した根拠にならない(trust on first use)。manifestは、Python本体・venv・アプリケーションのsource(editable installの `src/`)の3つのtreeの**完全な一覧**(全fileのsha256、全symlinkの参照先)で、一覧に無いentryがあればFAILになる。作成は、Cutover Safety Gateが全項目PASSした直後に行う。manifestを作るcommandはR7では提供していない(`walk_tree()` が一覧の形式を定める。作成手順は承認とあわせて決める)。
3. `/usr/bin/python3 -I -B scripts/cutover_readiness.py bootstrap --manifest <M> --manifest-sha256 <記録したhash> --python <venv>/bin/python3.14` がPASSであること。UNVERIFIEDはPASSではない。
- 止める条件: どちらかがPASS以外。

検査はすべて、検査対象のPython本体を起動する前に、別のinterpreter(macOSのsystem Python)で行う。検査対象のinterpreterで自分自身を検査する循環は無い。ただし検査器自身の信頼は、system Python(Apple署名)と、mainにcommitされた検査scriptに依存し、**mainはreviewの強制なしに変更できる(G-1、Blocker B-G1)**。したがって検査器の信頼はB-G1の解消(または受け入れ)に依存する。実行前に、使う検査scriptのsha256を承認記録の値と照合する。

起動時のcodeの検査範囲: `-I` は `sitecustomize`・`usercustomize` を無効にしない(無効にするのは `-S`)。検査は、計画した `sys.path` の全directory(標準library、`lib-dynload`、site-packages、各 `.pth` のpath行)に起動hookが無いことを確認し(manifestに載っていてもFAIL)、`.pth` のpathが3つのtreeの外を指せばFAIL、標準libraryのzip(`python314.zip`)があればFAILにする。`sys.path` はsite.pyの規則から算出したもので、interpreterを起動して観測したものではない。

## 5. Backup readiness

- ローカル: `.venv` は切替のどの段階でも変更しない。gate 8のfingerprintと `pip freeze` のhashを切替前に記録する(rollbackの比較元)。
- 本番Notion: **runtimeの切戻しではNotion上のデータは戻らない。** 切替後に書き込みcommand(`import`(`--dry-run` なし)、`--apply*` 付きの各command)を実行する予定があるなら、対象データベースのexport等、Notion側の復旧手段が用意されていることを人間が確認する。R7はこの確認をしていない(Notionへの接続は禁止)。
- 止める条件: Notion側の復旧手段を確認できないまま、書き込みcommandを実行する計画がある。

## 6. Preflight checks

`/usr/bin/python3 -I -B scripts/cutover_readiness.py launch-plan --manifest <M> --manifest-sha256 <H> --python <venv>/bin/python3.14 --config <設定file> -- <command> [args]` が `status: PASS` を返すこと。検査内容:

| 検査 | 合格条件 |
|---|---|
| bootstrap | 上記4の3がPASS |
| interpreter_explicit | 明示した絶対pathが、manifestのinterpreterと完全に一致(PATHから探さない) |
| inherited_environment | shellに `NOTION_*`・`PYTHON*`・`DYLD_*`・`LD_*`・`PIP_*` が無い(落とすのではなく拒否する) |
| config | 設定fileが通常file(symlink不可)、実行者の所有、group・otherの権限なし、`KEY=VALUE` 形式、既知の項目だけ、必須項目あり、data source idの形式 |
| dotenv_disabled | venvのpython-dotenvが1.2.1で、`dotenv/main.py` がmanifestで固定されたfileと同じで、構文解析(import・実行はしない)の結果、`load_dotenv()` が `PYTHON_DOTENV_DISABLED` を読む関数を呼んでいる |
| command | 既知の12 commandのいずれか |

すべてがPASSでなければ計画は作られない(未実施の検査がある場合は `incomplete preflight` でFAIL)。出力には値が含まれない(`credentials: <redacted>`、環境変数は名前だけ)。

**R7の時点でlauncherは計画を作るだけで、実行しない(`EXECUTION_ENABLED = False`、実行のcodeは存在しない)。** 切替時の起動は、計画の `argv`(`<python> -I -B -m mtg_notion_manager.cli <command> ...`)と環境変数の名前一覧に従い、人が明示的に行う。これを実行可能な部品にするかどうかは、別の承認事項(Blocker B-C23)。

`-I` は `PYTHON*` 環境変数・user site・scriptのdirectoryを無効にするが、site-packagesの `.pth` は引き続き実行される。環境変数の隔離は、OSのprocess隔離・filesystem隔離・network隔離のいずれでもない(CLIは本番でNotionへ接続する必要がある)。

## 7. Cutover execution boundary

- 境界の内側: 起動方法の変更だけ(`.venv/bin/mtg-notion-manager` → 上記6の計画どおりの起動)。PATH・shell設定・`.env`・`src/` は変更しない。
- 境界を越える最初の操作: 読み取りのみのcommand(`doctor`)。書き込みcommandは、下記8がすべて合格した後に限る。
- 実行するたびに、command名と引数(値を含めない形)、日時、終了コードをjournalに記録する。rollbackの判断(外部状態の扱い)に使う。

## 8. Post-cutover health checks

`docs/local-python-runtime.md`「切り替え後の確認項目」に加えて:

- `doctor` が旧環境と同じ結果。
- 読み取り専用command・dry-runの出力(件数・分類)が旧環境と一致。`--error-json` の出力形式が変わらない(Error Contract)。
- Pythonの例外(Traceback)が無い。

## 9. Abort criteria

次のいずれかで、その場で止めてrollbackの判断(10)へ進む: 上記4・6のPASS以外、health checkの失敗、旧環境と異なる結果、想定外の確認prompt・終了コード、Notionへの想定外の書き込みの兆候、journalの記録漏れ。模擬環境では、新runtimeのhealth check失敗で自動的にrollbackへ進み、切替前の状態(旧runtime、file内容同一)へ戻ることを確認した(`test_new_runtime_start_failure_rolls_back_to_the_pre_cutover_state`)。

## 10. Rollback decision

`R7_ROLLBACK_RUNBOOK.md` に従う。要点: ローカルの切戻し(起動方法を `.venv` に戻す)と、外部状態(Notion)の復旧を分けて判断する。切替後に書き込みの可能性があるcommandが1回でも実行されていれば、rollbackは**無条件ではない**(Notion上の変更は戻らない)。

## 11. Recovery verification

`R7_ROLLBACK_RUNBOOK.md`「復旧成功の判定」に従う。

## 12. Evidence retention

- 証跡(gate report、bootstrap・launch-planの出力、journal、health checkの結果)は、`verify_cutover_gate.py --export-evidence` と同じ規則(home・リポジトリのpathを匿名化、環境変数は名前だけ、値・secret・production identifierを含めない)で `docs/evidence/` に保存する。
- 一時directoryだけに証跡を置かない(R6で証跡を失った教訓)。
- 消失した証跡を復元したと記録しない。

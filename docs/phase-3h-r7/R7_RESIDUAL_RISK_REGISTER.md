# Phase 3H-R7 Residual Risk Register

2026-11-15時点。各リスクの意味は過去のPR本文(#47〜#51)と `docs/local-python-runtime.md` の記載から確認したもので、IDから推測したものは無い。`UNVERIFIED` は証跡が無いこと、`FAIL` は検証して不合格と判明したことを表す。

- Owner(全件): リポジトリ所有者(本番CLIの運用者でもある)。
- Acceptance authority(全件): 人間の承認者によるHuman Acceptance。R7の実装者・監査agentには受け入れの権限が無い。

## Cutover Blockers(未解消。Production Cutover Authorizationの前に人間の判断が必要)

| ID | 内容 | 解消の条件 |
|---|---|---|
| B-G1 | G-1: GitHubでreviewが技術的に強制されていない(必須approval 0、collaborator 1名) | 強制する構成の導入、または補償統制としての明示的な受け入れ(`R7_GITHUB_GOVERNANCE_AUDIT.md`) |
| B-D1 | D-1(ローカル範囲): 本番runtimeのmanifestと、その独立に記録されたsha256が無い。第三者buildのprovenanceは人間がまだ受け入れていない | manifestの作成、承認記録へのhashの記録、bootstrap検査のPASS、provenanceの受け入れ |
| B-C23 | C-2/C-3: 本番launcherは無効化された計画の部品だけで、実行可能な隔離起動経路は無い | 実行方法(手動の `env -i` 起動か、実行可能なlauncherか)の承認と、その検証 |
| B-RB | 外部状態: 本番Notionのbackup・復旧手段が確認されていない(R7はNotionに接続できない) | 書き込みcommandを実行する前に、人間がNotion側の復旧手段を確認する |

## Register

| Risk ID | Description | Previous status | Current evidence (R7) | Likelihood | Impact | Mitigation | Residual severity | Cutover blocker | Required next action |
|---|---|---|---|---|---|---|---|---|---|
| D-1(CI) | CIの `setup-python` がdownloadするinterpreterがhash検証されない(PR #47〜#49。pip upgradeはR3でhash検証済みのpipに置換済み) | minor, carried over | 変更なし。`ci.yml` の該当stepは同じ | 低(GitHub/setup-pythonの供給網が前提) | lint・type-check・generation-workspaceのjobのみ。secretなし | testはdigest固定のimage(Layer B)で実行 | minor | No(本番runtimeとは別) | Supply Chainの別Work Unit |
| D-1(local) | ローカル本番runtime(第三者build)のbootstrapの信頼境界 | not resolved(R6) | pre-launch検査を実装・test(下記R7の追加)。実runtimeへの適用は未実施、独立のmanifest hashなし → **UNVERIFIED** | 中(実行fileの差し替えには利用者権限が要る) | 本番の認証情報を持つprocessでのcode実行 | gate 1〜4(archiveの真正性・再compile比較)、R7のmanifest検査 | major | **Yes(B-D1)** | 上表B-D1 |
| G-1 | 必須checkとguardをPR自身が変更できる。reviewの強制が無い | accepted design limitation | live設定で必須approval 0・CODEOWNERSなしを確認 → **FAIL** | 中 | 未reviewの変更がmainに入る | 直接push・force push・削除はrulesetで拒否。CIのguardが変更を可視化 | major | **Yes(B-G1)** | 上表B-G1 |
| O-1 | `test_cli_verify_import.py::test_report_path_is_printed` が `COLUMNS` なしで失敗する(rich の折り返し) | info, out of scope | R7の `env -i`(`COLUMNS` なし)実行でPASS(3.14で単独・全体とも)。docsには折り返しに依存しない検査へ修正済みとの記載がある | 低 | testのみ | 修正済み(docs記載) | info | No | 次回のrisk棚卸しで「解消」とするか人間が判断 |
| C-2/C-3 | shellの `NOTION_*` が `.env` より優先される。console scriptは `-I` なしで起動するため `PYTHON*` がCLIに届く | 提案のみ・未適用 | 振る舞いは変更なし(`src/` 不変、`tests/test_config_env_precedence.py` が現状を記録)。R7で隔離起動の計画を実装・test(継承変数の拒否、`.env` 自動探索の無効化を実プロセスで確認) | 中 | 誤った認証情報・コードで本番に接続 | R7の計画(無効状態) | major | **Yes(B-C23)** | 上表B-C23 |
| Click 9 | typer 0.23.2 がClick 9.0で削除予定のAPI(`click.utils.get_binary_stream` 等)を使う | known | R7の実行でもDeprecationWarning 2件を観測。lockはclick 8.5.0固定 | 低(lockを更新しない限り発現しない) | CLIの起動不能 | lock固定 | minor | No | click/typer更新時に対応 |
| Timing tests | 秒単位のtimestampのfile名に依存するtest(`test_plan_title_updates_eligible_error_json_flag_unchanged`、R6のCIで `test_apply_price_link_dedupe_error_json_dry_run_matches_human_output`) | pre-existing flake | **R7で1件発現**: `af20d5e` のPython 3.14全体実行で `test_plan_title_updates_eligible_error_json_flag_unchanged` が1件失敗(exit 1。2つの出力の差はreport file名のtimestamp 192221 / 192222の1秒だけ)。この全体実行の記録は失敗のまま残す。同じclone内での同file単独の再実行3回(各132 passed)は補足であり、失敗の記録を置き換えない。後続の別commitの全体実行(`c68c160`・`f4b3eaa`・`1251bbd`、3.14・3.9)はいずれもPASSだが、これは別の実行であり、flakeの解消を示さない(`docs/evidence/phase-3h-r7/EVIDENCE.json` の `local_regression.runs`)。test自体は未修正。訂正: 本行は当初「R7の全体実行(3.14・3.9)ではいずれもPASS」と記載していたが、`af20d5e` の失敗と矛盾するため撤回した(最終SHA独立監査の指摘D-1) | 低〜中(秒の境界をまたぐと再発しうる) | testの偶発的失敗のみ(製品の振る舞いには影響しない) | 失敗した実行は失敗として記録し、再実行は補足扱い | minor | No | 別Work Unitでtimestamp依存を除去する |
| `.pth` execution | interpreter起動時に `.pth` の `import` 行が実行される | accepted with explicit limitations | `.venv-py314-r5` の `.pth` は2件、`import` 行0(path行のみ)。`.venv` には `distutils-precedence.pth`(`import` 行1、切替対象ではない)。R7の検査は一覧外の `.pth`・内容の変更・`import` 行を含む `.pth` をすべてFAILにする(再監査N-3) | 低 | 起動時のcode実行 | R7のmanifest検査(内容の固定) | minor | No(B-D1の一部として扱う) | manifestに `.pth` のhashを含める |
| Marshal Reader / 3.14.10 | readerはCPython 3.14.10のmarshal形式だけを扱う | accepted (maintenance) | 変更なし。R7の `KNOWN_VERSIONS` も3.14.10だけ(未知のversionはFAIL) | — | version更新時にgate 4がREJECTED/BLOCKED(安全側) | fail closed | minor | No(3.14.10の間) | Python更新時にreaderとtestを見直す |
| `sandbox-exec` deprecation | gate 4の隔離がApple非推奨の仕組みに依存 | accepted | 変更なし(R7ではgateを再実行していない) | 将来 | gate 4がBLOCKED(安全側) | probeで事前確認 | minor | No | 代替の隔離手段の検討 |
| N-1 | export時のe-mail検査が `%40`・全角・非ASCIIのaddress、実名を検出しない | not fixed | 変更なし。R7の証跡は同じ規則で作成し、別のpatternでも走査する | 低 | 証跡への個人情報の混入 | 人による走査 | minor | No | 検査の拡張 |
| N-2 | macOSで `/tmp` のarchiveの実path `/private/tmp` が過剰に匿名化される | not fixed | 変更なし | — | pathの判読性のみ。漏洩なし | — | info | No | なし |
| m-A | gate 8の `pip freeze` がpip.confを読む | not fixed | 変更なし | 低 | 偽PASSは実用上困難(fingerprintとfreeze hashの固定) | 固定hash | minor | No | R5基準hashを変えない形での修正を検討 |
| F01 | R6の検証中に `.venv-py314-r5` へpytestのcache 38件が書かれた | REMEDIATED / HISTORICAL EVIDENCE PARTIALLY LOST | R7の前後snapshotで `.venv-py314-r5`(5409 entries)・`.venv`(3607)・Python本体(2278)が不変(記録先は `docs/evidence/phase-3h-r7/EVIDENCE.json` の `preservation`。R7の証跡commitで追加)。消失した記録は再構成していない | — | — | 再発防止(`PYTHONDONTWRITEBYTECODE` 等)をR7の全実行で適用 | info(historical) | No | なし |

## R7で新たに記録するもの

| Risk ID | Description | Evidence | Residual severity | Cutover blocker | Required next action |
|---|---|---|---|---|---|
| R7-1 | `generation-workspace` jobが必須status checkではない | live ruleset | minor | No(製品機能と無関係) | 必須化するか人間が判断(GitHub設定の変更は別承認) |
| R7-2 | 起動時に実行されうるcodeの検査範囲。**独立監査M-1(major)**: 当初の実装はsite-packagesだけを走査し、標準library・`lib-dynload`・`.pth` が追加するpath(editable installの `src/`)の `sitecustomize`、manifestに無いfileを見逃していた(`-I` は `sitecustomize` を無効にしない)。修正: 3つのtreeの完全な一覧(一覧外のentryはFAIL)、計画した `sys.path` の全directoryの走査、treeの外を指す `.pth` のFAIL、終了時の再走査。**再監査N-1・N-2(major)**: tree内のdirectoryへのsymlinkは中身を検査されずに信頼され、`pyvenv.cfg` の `home` が検証したruntimeに結び付いていなかった。修正: symlinkはtree内の通常fileを指すものだけ許可、`sys.path` の各directoryはtree内のsymlinkを経由しない実directoryであること、`home` = runtimeの `bin`・`executable` もruntimeのもの・未知のkeyはFAIL・runtimeの一覧に `bin/python3.14` があること。再監査N-3: `.pth` の `import` 行は一切受け付けない。**第2再監査NEW-1(major)**: `pyvenv.cfg` の解析がCPythonのgetpath(最初の `home` 行を採用、keyの大文字小文字を区別しない)と異なり、重複した `home` で検査とinterpreterが別の値を読みえた。標準libraryの目印(`lib/python3.14/os.py`)・`lib-dynload` が無い場合も検査を飛ばしていた。修正: `pyvenv.cfg` は `\n`(`\r\n`)以外の改行・制御文字、重複key、小文字以外のkey、`key = value` 以外の行をFAIL。runtimeの一覧に `lib/python3.14/os.py` が必須、`bin` の下の目印(`bin/lib/`・`bin/pybuilddir.txt` 等)はFAIL、標準library・`lib-dynload` が無ければFAIL。**第3再監査N4-1・N4-2(major)**: `home` を字句的な `normpath` の後で比較していたため、`/tmp/../<runtime>/bin` のように正規化すると一致するがgetpathは別の場所(world-writableな `/tmp`)を探すpathが通った。`._pth`(`sys.path` を丸ごと置き換える)を拒否していなかった。修正: `home`・`executable` は文字列として完全一致、manifestのrootは正規化済みの絶対path、`runtime_root` はsymlinkを含まない(realpathと一致)、3つのtreeのどこかに `._pth` があればFAIL、`venv/bin/pyvenv.cfg` はFAIL、interpreterは `bin/python3.14` に固定(N4-3)、`__PYVENV_LAUNCHER__` も拒否(N4-5)。**第4再監査NEW-A(major)**: 名前の照合が大文字小文字を区別していたが、macOSの既定のvolumeは区別しない(getpath・site.pyは固定の小文字の名前で開く)。`PYTHON3.14._PTH`・`bin/PyVenv.cfg`・`lib/Python3.14/site-packages` が見逃されえた。修正: 名前の照合はすべてNFD正規化+casefold後に行い、treeの中で正規化後に衝突する名前、特別な名前(`bin`・`lib`・`site-packages`・`lib-dynload`・`pyvenv.cfg`・`__pycache__` 等)の別の綴り、`lib/` の下の `python3.14` 以外の `python*` をFAIL。第4再監査NEW-B(info): `.pth` の先頭のBOMはpathとして扱われ、存在しないdirectoryとしてFAIL(fail closed) | `verify_bootstrap()`、`tests/test_cutover_readiness_bootstrap.py` | 修正後の残余: minor(`sys.path` はsite.pyの規則から算出したもので、起動して観測したものではない。frozen module・interpreterの組み込み設定、実行fileが読み込む動的library(`otool -L` 相当)は対象外。実行fileそのものはgate 5でarchiveの複製であることを確認する)。getpath・site.pyの挙動(最初の `home` の採用、字句的な上方探索、`._pth` の探索順、`site.venv()` の `pyvenv.cfg` の探索順)の一部は、監査agentの記憶に基づくもので、CPython 3.14のsourceとの照合はしていない。規則はいずれも拒否側(fail closed)に倒している | Yes(B-D1の一部) | B-D1の解消時に、実runtimeで算出した `sys.path` と起動後の `sys.path` を隔離環境で比較する |
| R7-3 | 検査と実際の起動の間のTOCTOU。計画は実行しないため、検査後にfileが変わりうる | 設計上 | minor | No(B-C23の一部) | 実行可能にする場合は、検査の直後に同じprocessから起動し、再検査する |
| R7-4 | launcherの設定fileは `KEY=VALUE` の厳格な形式で、dotenvの引用符・`export` を受け付けない | `load_config()` | info | No | 運用者が専用fileを作る(`.env` を複製しない) |
| R7-5 | 独立監査は実装と同じAIモデル・同じ運用主体による別context | 監査記録 | — | No | 人間によるreviewが最終的な独立性を担う |
| R7-6 | provenanceのPASSは「人が記録したattestationの証跡(commit済みのEVIDENCE.json、sha256固定)が、このarchiveと全gateのPASSを記録している」ことの確認で、attestationをこの検査で再検証するものではない。archiveと実runtimeの対応はgate 4が示す | `check_provenance()` | minor | No(B-D1の一部) | provenanceの受け入れは人間が判断する |
| R7-7 | `Secret` をpickle・copyすると値が保持される(出力経路ではない) | 監査info | info | No | なし |
| R7-8 | 厳格な規則によりFAILになる(fail closed)構成: symlinkで作られたvenv、Linuxの `lib64` symlink、uv等が書く追加の `pyvenv.cfg` key、`app_root` 以外(例: `tools/generation_workspace`)を指す `.pth`、path中の `マイドライブ` のUnicode正規化(NFC/NFD)の違い。実runtimeと `.venv-py314-r5` は上記に該当しないことを読み取りで確認(`.pth` は2件のうち1件が `tools/generation_workspace` を指すため、manifestの `app_root` の決め方を承認時に定める必要がある) | 第2再監査、R7の読み取り確認 | minor | No(B-D1の一部) | manifestの作成手順で、treeの範囲(`app_root`)を決める |

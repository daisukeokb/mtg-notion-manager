# ローカル実行環境のPython 3.14移行(Phase 3H-R5)

CIとLayer B imageはPhase 3H-R4でCPython 3.14.10になった。手元のmacOSのCLI環境(`.venv`、CPython 3.9.6)はそのまま残し、別の新しい環境(`.venv-py314-r5`)を作って検証する。本番CLIの切り替え(下記「切り替え手順」)は、別途Human Acceptanceを得てから実施する。PRのmergeは切り替えの承認ではない。

## 構成

| 項目 | 内容 |
|---|---|
| Python本体 | CPython 3.14.10、[python-build-standalone](https://github.com/astral-sh/python-build-standalone) release `20261026` の `cpython-3.14.10+20261026-aarch64-apple-darwin-install_only.tar.gz`(Apple Silicon用) |
| 本体の検証 | sha256 `30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4` を、releaseの `SHA256SUMS`・GitHubのasset digest・SLSA provenance attestation(`gh attestation verify ... -R astral-sh/python-build-standalone`、署名者 `.github/workflows/release.yml@refs/heads/main`)の3つと照合。展開前に、絶対path・`..`・外を指すlink・device fileが無いことを確認 |
| 本体の場所 | `~/.local/share/mtg-notion-manager/runtimes/cpython-3.14.10+20261026/python/`(PATHに入れない。システムPython・Homebrew・shell設定は変更しない) |
| 仮想環境 | リポジトリ直下の `.venv-py314-r5/`(`.gitignore` の `.venv-*/`)。`scripts/setup_local_venv.py` で作る |
| 依存関係 | CIと同じ `requirements/ci-lock.txt`(hash固定、32件)と `requirements/ci-pip-lock.txt`(pip 26.0.1)。lockはLinux x86_64向けに解決したものだが、各versionの公開ファイルすべてのhashを持つため、macOS arm64のwheelもhash検証される |
| 既存環境 | `.venv`(CPython 3.9.6、Command Line Toolsの `/Library/Developer/CommandLineTools/usr/bin/python3` ベース)は変更しない |

## 作成と確認

```bash
PY314="$HOME/.local/share/mtg-notion-manager/runtimes/cpython-3.14.10+20261026/python/bin/python3.14"
"$PY314" -I scripts/setup_local_venv.py .venv-py314-r5 --dry-run   # 作成前の確認のみ
"$PY314" -I scripts/setup_local_venv.py .venv-py314-r5
.venv-py314-r5/bin/python -I scripts/check_local_runtime.py
```

- `setup_local_venv.py` はCIと同じ手順(hash検証済みpipのwheelから実行したpipでpipを入れる → lockを `--require-hashes --no-deps --only-binary=:all:` → 2つのローカルprojectを `--no-deps --no-build-isolation --no-index -e` → `pip check`)で入れる。作成先が既に存在すれば何もせず失敗する。pipはユーザーのpip設定・`PIP_*`・`PYTHON*`・`NOTION_*` 環境変数なしで実行する。途中で失敗した場合、作りかけ(空のこともある)の作成先が残り、再実行は既存pathとして拒否されるので、内容を確認してから手で削除して作り直す。
- `check_local_runtime.py`(読み取り専用)は、CPython 3.14.10の仮想環境であること、入っている配布物がlockと完全に一致すること(余分・不足・version違いなし)、`pip check` を確認し、CLIと12個のcommandの `--help` をプロセス内で表示する。CLIの確認はCPythonのaudit hookの下で行い、`.env*` という名前のファイルを開くこと、socketの接続・送信・bind・名前解決、プロセスの起動を、実行される前に拒否して失敗にする(`--help` はcommand本体より前に処理されるため `Config.load()` には到達しない。それを前提ではなく観測結果にするための確認)。
- テスト一式は、`.env` を含まないcommit済みtreeの複製(`git init` + `git fetch --depth=1 file://<repo> <HEAD>`)と、そのtreeから作った別の仮想環境で実行する(CIのLayer Bと同じく、ホストの環境変数は引き継がずダミーの `NOTION_*` だけを渡す)。手元ではDockerが無いためLayer B(と red check)は実行できず、その検証はCIで行う。

### 注意: `.env` の探索

`Config.load()` は引数なしの `load_dotenv()` を呼び、python-dotenvは呼び出し元moduleのdirectoryから上へ `.env` を探す。`.venv-py314-r5` のeditable installは `.venv` と同じリポジトリの `src/` を指すため、**新しい環境でcommand本体を実行すると、既存環境と同じリポジトリ直下の本番 `.env` を読む**。認証情報の複製・移動は不要だが、裏返すと、新しい環境で `--help` 以外のcommandを実行することは本番設定を使うことであり、切り替えの承認後にだけ行う。

## 切り替え手順(Human Acceptance後のみ)

0. mainに本変更が入っていること、作業treeが意図したbranch・commitであり、`git status --short -- src tools requirements scripts pyproject.toml config` が空であることを確認する。確認できなければ中止。どちらの環境もeditable installなので、checkoutしているコード(と作業directory相対の `config/`・`reports/` 等)がそのまま使われる。
1. 下記「切り替え前の安全ゲート」の全項目を確認し、結果を記録する。全項目が合格でなければ、手順2以降へ進まない。
2. 切り替えの直前に、`.venv-py314-r5/bin/python -I scripts/check_local_runtime.py` が成功することを再度確認する。失敗すれば中止。
3. 起動方法を切り替える。PATHやshell設定は変更せず、明示的なpathで起動する。
   - 変更前: `.venv/bin/mtg-notion-manager <command> ...`(または `source .venv/bin/activate` 後に `mtg-notion-manager`)
   - 変更後: `.venv-py314-r5/bin/mtg-notion-manager <command> ...`(または `source .venv-py314-r5/bin/activate`)
4. 書き込みのないcommandから確認する: `doctor`(Notionの読み取り)、`import <url> --dry-run`、`verify-import <url>`、`audit-duplicates` など。可能なら同じ入力で旧環境(`.venv`)の出力と比較する。
5. 書き込みを伴うcommand(`--apply` 等)は、手順4がすべて期待どおりだった後にだけ使う。

### 切り替え前の安全ゲート(Cutover Safety Gate)

本番CLIを切り替える前に、次の8項目をすべて確認する。**いずれかがFAIL・BLOCKED・NOT VERIFIED(確認できない、ツールやネットワークが使えない、実施を省略した場合を含む)なら、切り替えを禁止する。** 警告として記録して続行してはならない。

8項目は `scripts/verify_cutover_gate.py` が文書の順(手順0→1→2→3→4→5→6・7→8)に実行する。archiveの取得(項目1の確認方法の `gh release download`)だけは人が先に行う:

```bash
/usr/bin/python3 -I -B scripts/verify_cutover_gate.py --archive <archive> --report <リポジトリ外の新しいfile>
```

各項目は、それより前の項目がこの実行の中ですべて合格していなければ実行されない(script内で強制し、呼び出されても `GateOrderError` で止まる)。最初の不合格(FAIL・BLOCKED)で止まり、残りは `NOT_RUN` になる。以前の実行結果は再利用しない。終了コードは、全項目合格なら0、FAILがあれば1、BLOCKED等なら2。scriptはインストール済みのCPython 3.14以外のinterpreter(macOSのsystem Python等)で実行する。インストール済みのPython本体を最初に実行するのは項目5で、項目1〜4がすべて合格した後である。`<archive名>` は上記「構成」のarchiveのfile名、`<archive>` は取得したfileのpath、`$PY314` は上記「作成と確認」のPython本体。

| # | 確認項目 | 確認方法 | 合格条件 | 実行方法 |
|---|---|---|---|---|
| 1 | Python配布archiveのSHA-256 | 同じarchiveを、リポジトリ外の新しい空directoryへ取得し直す(`gh release download 20261026 -R astral-sh/python-build-standalone -p '<archive名>' -D <dir>`)。`shasum -a 256 <archive>` | 上記「構成」のsha256と一致。`~/.local/share/mtg-notion-manager/runtimes/cpython-3.14.10+20261026/ARCHIVE_SHA256` もこの値と一致(このfileは本体と同じくユーザーが書き込めるため、基準にしない) | script(archiveの取得は手動) |
| 2 | Release assetのdigest | `gh api repos/astral-sh/python-build-standalone/releases/tags/20261026 --jq '.assets[] \| select(.name=="<archive名>") \| .digest'` | `sha256:` に続く値が同じsha256 | script |
| 3 | Provenance | `gh attestation verify <archive> -R astral-sh/python-build-standalone --signer-workflow astral-sh/python-build-standalone/.github/workflows/release.yml` | 終了コード0。署名者が `.github/workflows/release.yml@refs/heads/main` | script |
| 4 | ローカルのPython本体 | archiveの全memberを検査したうえで、インストール済みのtreeと比較し、すべての `.pyc` を検証済みのsourceから隔離環境で再compileして比較する(下記)。インストール済みのPython本体はこの項目では実行しない | 下記「項目4の判定」のすべて | script |
| 5 | 仮想環境の実行file解決先 | `"$PY314" -I -B -c 'import platform; print(platform.python_implementation(), platform.python_version(), platform.machine())'` と `.venv-py314-r5/bin/python -I -B -c 'import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)'` | `CPython 3.14.10`。`sys.executable` と `sys.prefix` がリポジトリの `.venv-py314-r5` の中。`sys.base_prefix` が上記「構成」の本体の場所 | script |
| 6 | 依存関係lockとの整合 | `.venv-py314-r5/bin/python -I scripts/check_local_runtime.py` | 終了コード0 | script(`check_local_runtime.py` を呼ぶ。配布物の名前とversionの一致まで。installed fileの内容のhashは確認しない) |
| 7 | `pip check` | 同上 | 同上 | script |
| 8 | 既存のPython 3.9環境が変更されていない | `.venv/bin/python --version`、下記のfingerprint、`pip freeze` のhash | `Python 3.9.6`、かつ両方のhashが下記の値と一致 | script |

**項目4の判定**(Phase 3H-R6で定義を修正。理由は下記「既知の差異・残課題」):

1. archiveの全memberの検査: 絶対path・`..`・`python/` 以外の場所・重複member・symlinkのmemberの下のmember・archive外を指すsymlink・hardlink・通常file/directory/symlink以外の種類・setuid/setgid/sticky・誰でも書き込めるmodeが1件でもあればFAIL。
2. `.pyc` 以外の全member(1,698件)について、インストール済みの対応entryと、種類・内容のsha256・symlinkの参照先・実行bitが一致すること。symlinkは追わない(`lstat`)。不足があればFAIL。
3. archiveに無いentryは、archiveのdirectoryの下の `__pycache__` directory(`__pycache__` の中の `__pycache__` は不可)と、`<名前>.cpython-314[.opt-1|.opt-2].pyc`(対応する `.py` がarchiveにあり、実行bitの無い通常file)だけを許す。それ以外(symlink、拡張module、別versionのcache等)はFAIL。生成された `.pyc` は件数を記録する。
4. すべての `.pyc`(archive同梱の3件と、実行時に生成されたもの)を再compileして比較する: 検査済みのarchiveを一時directoryへ安全に展開し(上書き・link追従なし)、その展開先のCPython 3.14.10を、macOSのsandbox(`sandbox-exec`。network・home directoryの読み取り・一時directory外への書き込みを拒否)の中で `-I -B -S`・最小限の環境変数で実行する。sandboxが使えない、または使う前の検査(networkへの接続・home directoryの読み取り・外への書き込みが拒否され、一時directoryへの書き込みはできること)で確認できなければBLOCKED。再compileは `py_compile`(timestamp方式、file名の `opt-N` に対応する最適化level、`dfile` はインストール済みのsourceのpath、出力は一時directoryの中だけ)で行い、sourceの更新時刻はインストール済みのsourceに合わせる(一時directoryの複製だけを変更する)。
5. 展開先のinterpreterがCPython 3.14.10(cache tag `cpython-314`)で、インストール済みの `.pyc` のmagic numberがそれと同じであること。そのうえで、各 `.pyc` を次のいずれかに分類し、すべてが `BYTE_IDENTICAL`・`MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF`・`MARSHAL_EQUIVALENT_APPROVED_INTERNING_DIFF` のいずれかであること。1件でも `STRUCTURAL_MISMATCH` か `REJECTED` があればFAIL(差分の位置・sha256・compile条件・構造上の差分を記録する。修復しない)。
   - `BYTE_IDENTICAL`: 再compileの結果がインストール済みの `.pyc` とheader(magic number・flags・sourceの更新時刻と大きさ)を含めてbyte単位で一致する。原則はこれ。
   - `MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF`(Human Acceptance Decision 03): byte単位で一致しないfileだけを、scriptのCPython 3.14.10用marshal reader(marshalを読み込まず、実行もしない。対応する型は下記)で両方とも最後まで解析し、headerが同一で、`TYPE_REF` を解決した後の構造(code objectの全field・定数・入れ子のcode・文字列のinterned等の種類を含む)が同一であるもの。違いはmarshalの `FLAG_REF` と参照の番号だけ。さらに、同じ実行の中で、展開先のinterpreter自身がsandbox内で合成moduleをcompile(実行はしない)・marshalした出力に対してreaderを検査し(全fieldの読み取り、参照配置だけが違う出力の同等性、file名・定数が違う出力の非同等性)、合格していること。対象はDecision 03で承認された4件(`argparse`・`inspect`・`typing`・`weakref` の `cpython-314.pyc`)で、インストール済みのfileのsha256がそのときの値と同じものに限る。他のfileや内容が変わったfileには、同等でも適用しない(`REJECTED`)。
   - `MARSHAL_EQUIVALENT_APPROVED_INTERNING_DIFF`(Human Acceptance Decision 04): 構造の違いが、承認された5つの文字列のinternedの印だけであるもの。対象は `argparse`(`HelpFormatter._Section`・`_SubParsersAction._ChoicesPseudoAction`)・`inspect`(`_signature_fromstr.<locals>.RewriteSymbolics`)・`typing`(`NewType.__mro_entries__.<locals>.Dummy`)・`weakref`(`finalize._Info`)で、インストール済みのfileのsha256がそのときの値と同じものに限る。各文字列は、scriptに記録した位置にあるclass本体のcode objectの定数0(`codegen_class_body` が `__qualname__` に格納する定数。そのcodeの `co_qualname` が同じ値で、`co_names` に `__qualname__` があること)で、インストール済み側が `TYPE_SHORT_ASCII_INTERNED`(`Z`)、再compile側が `TYPE_SHORT_ASCII`(`z`)、値が完全に同じであること。その位置だけを `z` に置き換えると、headerと構造(全code field・bytecode・他の定数)が完全に一致すること。readerの検査が合格していること。byte単位の一致でも、実行時のobjectの同一性でもない(インストール済み側の文字列はinternされ、再compile側はされない。値は同じ)。他のfile・文字列・位置には適用しない。
   - `STRUCTURAL_MISMATCH`: 解析できたが、headerまたは構造が違う。
   - `REJECTED`: 解析できない(未対応の型・未知のtag・不正な参照・途中で終わる・余分なbyte・上限超過等)、または同等だが承認の対象外・readerの検査が不合格。

   readerが受け付ける型は、compileされたmoduleに現れるものだけ(None・True・False・Ellipsis・int・float・complex・bytes・str・tuple・frozenset・slice・code)。list・dict・set・`TYPE_NULL`・version 0の表現・未知のtagや、読み込み途中のobjectへの参照はREJECTED。上限: 入力8 MiB、object数100万、入れ子の深さ100(参照を解決した後は150)、tuple・frozensetの要素数100万、str・bytesの長さ8 MiB、参照数100万、整数の桁数2,000(15 bit単位)。4件の大きさは27,707〜171,917 byte。frozensetは要素の順序も比較する(CPythonのwriterは要素を各要素のmarshal出力の順に並べるため、参照配置の違いで順序が変わり、同等でも `STRUCTURAL_MISMATCH` になる可能性がある。安全側の誤判定)。

インストール済みのPython本体・`.pyc`・2つの仮想環境は読むだけで、変更・削除しない。一時directoryは最後に削除する。再compileの一致は、archiveの真正性(項目1〜3)と組み合わせて意味を持つ(展開先のinterpreterも、比較元のsourceも、検証済みのarchiveのもの)。第三者buildそのものの安全性を示すものではない。

**項目8の基準値**(fingerprintはPhase 3H-R5の作業前の測定値。fingerprintは `.pyc`(`site-packages/__pycache__` は全体)と `bin/python*` のsymlinkを対象にしない。`pip freeze` のhashはPhase 3H-R5の完了時の測定値で、`-e` の2行以外は作業前と同じ):

```bash
(cd .venv && find . -path ./lib/python3.9/site-packages/__pycache__ -prune -o -name '*.pyc' -prune -o -type f -print0 | sort -z | xargs -0 shasum -a 256) | shasum -a 256
# c5e0c2be7730366ea961e699524232522383890d3d86f92eb2647cc851ee5087
.venv/bin/python -I -m pip freeze --all | grep -v '^-e ' | shasum -a 256
# 54d878d575ac887517cd30433dec08e1c4b03cb9862999b54ae498617680e7de
```

`pip freeze` のeditable installの2行 `-e git+...@<commit>` にはcheckout中のcommitが入るため、hashはその2行を除いて計算する。切り戻しの比較用に、`.venv/bin/python -I -m pip freeze --all` の出力もここで記録しておく(git remoteのURLを含むため、リポジトリの外に置き、共有しない)。

**不合格・未確認の場合**: Python本体の完全性、provenance、interpreterの同一性、依存関係の整合性、旧環境の保全のいずれかの確認が失敗した、または確認できない場合は、切り替えを中止する。自動で修復・再インストールをせず、旧環境を変更・削除せず、新たな明示的な承認を得るまで先へ進まない。この時点では起動方法を切り替えていないため、切り戻す作業は無く、引き続き `.venv` を使う。

**このゲートの限界**: `scripts/verify_cutover_gate.py` は確認のためのscriptで、切り替えを技術的に強制するものではない(`.venv-py314-r5/bin/mtg-notion-manager` は、このゲートを通さなくても起動できてしまう)。archiveの取得は人が行う。項目6・7は `scripts/check_local_runtime.py` に依存し(Pythonのversion・仮想環境であること・editable installの向き先・command一覧と、audit hookの下での各 `--help` の表示)、確認に使うscriptとlockは作業treeのもの(手順0の確認に依存する)。項目4の隔離はmacOSの `sandbox-exec` に依存する(Apple自身が非推奨としている仕組みで、将来使えなくなればBLOCKEDになる)。scriptを実行したshellの `PYTHON*`・`NOTION_*` 環境変数は、名前だけを報告する。

### 切り替え後の確認項目

- `check_local_runtime.py` 成功、`.venv-py314-r5/bin/python --version` が `Python 3.14.10`。
- `doctor` が旧環境と同じ結果(認証・両DB接続・必須スキーマ)。
- dry-run・読み取り専用commandの出力(件数・分類)が旧環境と一致する。`--error-json` の出力形式(schema_version等)が変わらない。

### 停止条件

切り替えの前は上記の安全ゲートに従う。切り替えの後は、次のいずれかで、その場で中止して切り戻す: `check_local_runtime.py` の失敗、Pythonの例外(Traceback)、`doctor` の失敗、旧環境と異なるdry-run結果、想定外の確認プロンプト・終了コード、`--error-json` の形式の違い、Notionへの想定外の書き込みの兆候。いずれの場合も、自動で修復・再インストールをせず、旧環境を変更・削除しない。切り替えをやり直すには、新たな明示的な承認を得る。

## 切り戻し(rollback)

- 既存の `.venv` は作成・検証・切り替えのどの段階でも変更しないため、`.venv/bin/mtg-notion-manager` での起動に戻すだけでよい(activateしていた場合は `deactivate` してから `source .venv/bin/activate`)。PATH・shell設定・起動scriptは変更していないので、戻す設定もない。
- 設定ファイル: `.env` は移動・複製・編集しないため、復旧作業は不要。切り戻しのために `.env` を複製しない。
- 切り戻しの前提(読み取りのみ): `scripts/verify_cutover_gate.py` は全項目が合格した場合に続けて、`.venv/bin/mtg-notion-manager` が存在しその1行目が `.venv` のinterpreterを指すこと、そのinterpreterが `Python 3.9.6` であること、旧環境でCLIの `--help` がaudit hookの下で表示できることを確認する(`rollback_readiness`)。本番での切り戻し自体は行わない。
- 確認: `.venv/bin/python --version` が `Python 3.9.6`、`.venv/bin/python -I -m pip freeze --all` が安全ゲートで記録した出力と(`-e` の2行を除いて)同じ、項目8の2つのhashが基準値と一致、`.venv/bin/mtg-notion-manager --help` が表示される。
- 新しい環境を消す場合(任意・ユーザー判断): `.venv-py314-r5/` と `~/.local/share/mtg-notion-manager/runtimes/cpython-3.14.10+20261026/` を削除する。Python本体は、それを基にした仮想環境(`.venv-py314-r5/` と、検証用に作った一時的な仮想環境)からだけ参照される。既存の `.venv` からは参照されない。
- 本番Notion上のデータを戻す手順はここには含めない(切り替え自体はデータを変更しない。書き込みcommandの結果はそれぞれのcommandの記録で扱う)。

## 既知の差異・残課題

- `requires-python = ">=3.9"` とruffの `target-version = "py39"` は、旧環境が残る間の下限として据え置く(切り替え完了後に別作業で見直す)。
- macOSでは `tmp_path` が長く、richがCLI出力のpathを80桁で折り返すことがある(`tests/test_cli_verify_import.py` のpath表示の検査は折り返しに依存しないよう修正済み)。
- Python本体はpython-build-standaloneのself-hosted runnerでbuildされたもの(provenanceの `runnerEnvironment`)。ad-hoc署名のため、Gatekeeperの公証(notarization)は無い。
- Google Drive同期フォルダ内に仮想環境を置く点は既存の `.venv` と同じ。
- pipのwheel取得のHTTPS確認はredirect後の最終URLに対して行う。proxy・CA関連の環境変数は引き継ぐ。完全性はsha256(pip wheel・lock)に依拠する。
- `tests/test_local_runtime_setup.py` の一部はscriptのsource文字列を検査するため、同等な書き換えでも失敗しうる。audit hookのネットワーク・プロセス拒否は判定関数の単体テストで確認している(子プロセスでの通し確認は `.env` の場合のみ)。
- Python本体に同梱されているpip(26.2.2)は、`setup_local_venv.py` からは実行しない。ただし環境の棚卸し時に一度だけ `python3.14 -I -m pip --version` で実行した(何もinstallしていない)。
- 項目4の定義の修正(Phase 3H-R6): 以前の定義は、archive同梱の `.pyc` 3件を他のfileと同様に内容で比較し、それ以外の `.pyc` を比較前に削除するものだった。しかしarchiveの `.pyc` は、build時のsourceの更新時刻(1793023270)を記録している一方、archive内のsourceの更新時刻は1704067200に正規化されているため、初回の実行でCPythonが作り直す。作り直した `.pyc` はsourceのpath(`co_filename`。archiveではbuild machineの一時path)も異なる。このため以前の定義は一度でも実行したruntimeでは必ずFAILし、削除も読み取り専用の確認と両立しない。修正後は、`.pyc` をすべて検証済みのsourceから隔離環境で作り直し、byte単位で比較する。
- marshalの出力の非決定性(Phase 3H-R6、Decision 03): 修正後の定義で、365件のうち4件(`argparse`・`inspect`・`typing`・`weakref`)がbyte単位で一致しなかった。CPythonのmarshalは、参照が1つだけのobjectには `FLAG_REF` を付けず、interned文字列には常に付けるため、出力がcompileしたprocessの参照数や文字列のinterned状態に依存する(`Python/marshal.c` の `w_ref`)。このためDecision 03で、上記の4件に限り、構造の比較(`MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF`)を認めた。その比較の結果、4件の違いは参照の配置だけでなく、入れ子のclassの `__qualname__` 定数の文字列(計5つ)のinternedの印だった(Decision 03の規則ではFAIL)。Decision 04で、その5つに限り `MARSHAL_EQUIVALENT_APPROVED_INTERNING_DIFF` を認めた。この許容は、D-1(bootstrapの信頼境界)の解消を意味しない。readerはCPython 3.14.10のmarshal形式(`PYC_MAGIC_NUMBER` 3627、marshal version 5)に依存するため、Pythonのversionを上げる場合はreaderとその検査を見直す必要がある(保守上の残リスク)。
- 環境変数(C-2・C-3): `load_dotenv()` は既に設定されている環境変数を上書きしないため、CLIを起動するshellに `NOTION_*` があれば `.env` より優先される(空の値でも `.env` の値は使われない)。console script(`.venv-py314-r5/bin/mtg-notion-manager` 等)はinterpreterを `-I` なしで起動するため、`PYTHONPATH` 等の `PYTHON*` 環境変数もCLIに影響する。どちらも合成値のテストで確認した(`tests/test_config_env_precedence.py`)。提案(未実施・未承認): 切り替え後の起動は、許可した環境変数だけを渡す形(例: `env -i HOME="$HOME" PATH=/usr/bin:/bin .venv-py314-r5/bin/mtg-notion-manager ...`)にする。

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

0. mainに本変更が入っていること、作業treeが意図したbranch・commitであり、`git status --short -- src tools requirements scripts pyproject.toml` が空であることを確認する。確認できなければ中止。どちらの環境もeditable installなので、checkoutしているコード(と作業directory相対の `config/`・`reports/` 等)がそのまま使われる。
1. 下記「切り替え前の安全ゲート」の全項目を確認し、結果を記録する。全項目が合格でなければ、手順2以降へ進まない。
2. 切り替えの直前に、`.venv-py314-r5/bin/python -I scripts/check_local_runtime.py` が成功することを再度確認する。失敗すれば中止。
3. 起動方法を切り替える。PATHやshell設定は変更せず、明示的なpathで起動する。
   - 変更前: `.venv/bin/mtg-notion-manager <command> ...`(または `source .venv/bin/activate` 後に `mtg-notion-manager`)
   - 変更後: `.venv-py314-r5/bin/mtg-notion-manager <command> ...`(または `source .venv-py314-r5/bin/activate`)
4. 書き込みのないcommandから確認する: `doctor`(Notionの読み取り)、`import <url> --dry-run`、`verify-import <url>`、`audit-duplicates` など。可能なら同じ入力で旧環境(`.venv`)の出力と比較する。
5. 書き込みを伴うcommand(`--apply` 等)は、手順4がすべて期待どおりだった後にだけ使う。

### 切り替え前の安全ゲート(Cutover Safety Gate)

本番CLIを切り替える前に、次の8項目をすべて確認する。**いずれかがFAIL・BLOCKED・NOT VERIFIED(確認できない、ツールやネットワークが使えない、実施を省略した場合を含む)なら、切り替えを禁止する。** 警告として記録して続行してはならない。

項目は1→2→3→4の比較→4のversion→5→6・7→8の順に実施し、最初の不合格で止める(項目1〜3と項目4の比較が合格するまで、Python本体を実行しない)。`<archive名>` は上記「構成」のarchiveのfile名、`<archive>` は取得したfileのpath、`$PY314` は上記「作成と確認」のPython本体。

| # | 確認項目 | 確認方法 | 合格条件 | 実行方法 |
|---|---|---|---|---|
| 1 | Python配布archiveのSHA-256 | 同じarchiveを、リポジトリ外の新しい空directoryへ取得し直す(`gh release download 20261026 -R astral-sh/python-build-standalone -p '<archive名>' -D <dir>`)。`shasum -a 256 <archive>` | 上記「構成」のsha256と一致。`~/.local/share/mtg-notion-manager/runtimes/cpython-3.14.10+20261026/ARCHIVE_SHA256` もこの値と一致(このfileは本体と同じくユーザーが書き込めるため、基準にしない) | 手動 |
| 2 | Release assetのdigest | `gh api repos/astral-sh/python-build-standalone/releases/tags/20261026 --jq '.assets[] \| select(.name=="<archive名>") \| .digest'` | `sha256:` に続く値が同じsha256 | 手動 |
| 3 | Provenance | `gh attestation verify <archive> -R astral-sh/python-build-standalone` | 終了コード0。署名者が `.github/workflows/release.yml@refs/heads/main` | 手動 |
| 4 | ローカルのPython本体 | `"$PY314" -I -c 'import platform; print(platform.python_implementation(), platform.python_version(), platform.machine())'`。あわせて、展開済みの各fileの内容をarchiveと比較する(下記) | `CPython 3.14.10 arm64`。比較で不一致(内容・symlinkの参照先・実行bit)・不足・archiveに無いfileが無い | 手動(比較用のscriptは未実装) |
| 5 | 仮想環境の実行file解決先 | `.venv-py314-r5/bin/python -I -c 'import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)'` | `sys.executable` と `sys.prefix` がリポジトリの `.venv-py314-r5` の中。`sys.base_prefix` が上記「構成」の本体の場所 | 手動(`check_local_runtime.py` は仮想環境であることだけを確認し、場所までは確認しない) |
| 6 | 依存関係lockとの整合 | `.venv-py314-r5/bin/python -I scripts/check_local_runtime.py` | 終了コード0 | scriptで確認(配布物の名前とversionの一致まで。installed fileの内容のhashは確認しない) |
| 7 | `pip check` | 同上 | 同上 | scriptで確認 |
| 8 | 既存のPython 3.9環境が変更されていない | `.venv/bin/python --version`、下記のfingerprint、`pip freeze` のhash | `Python 3.9.6`、かつ両方のhashが下記の値と一致 | 手動 |

**項目4の比較**: archiveには `python/lib/python3.14/encodings/__pycache__/` の `.pyc` 3件だけが含まれ、それ以外の `.pyc` は実行時に生成されたもので比較できない。比較の直前の準備として、Python本体のdirectory内にあるarchiveに含まれない `.pyc`(と空になった `__pycache__`)を削除し、archiveに含まれる3件は他のfileと同様に比較する(削除は、改変されたbytecodeが残らないようにするため。削除したものは次回の実行時に再生成される)。削除の前に、削除するfileの一覧と件数を記録する。archiveのpath `python/...` は、本体の場所の `python/...` に対応する。本体のdirectoryはユーザーが書き込めるため、作成時の検証だけでは後からの改変を検出できない。削除の対象はPython本体の `.pyc` だけで、`.venv` と `.venv-py314-r5` は対象にしない。比較の結果不一致だった場合に、修復や展開し直しをしない(下記)。

**項目8の基準値**(fingerprintはPhase 3H-R5の作業前の測定値。fingerprintは `.pyc`(`site-packages/__pycache__` は全体)と `bin/python*` のsymlinkを対象にしない。`pip freeze` のhashはPhase 3H-R5の完了時の測定値で、`-e` の2行以外は作業前と同じ):

```bash
(cd .venv && find . -path ./lib/python3.9/site-packages/__pycache__ -prune -o -name '*.pyc' -prune -o -type f -print0 | sort -z | xargs -0 shasum -a 256) | shasum -a 256
# c5e0c2be7730366ea961e699524232522383890d3d86f92eb2647cc851ee5087
.venv/bin/python -I -m pip freeze --all | grep -v '^-e ' | shasum -a 256
# 54d878d575ac887517cd30433dec08e1c4b03cb9862999b54ae498617680e7de
```

`pip freeze` のeditable installの2行 `-e git+...@<commit>` にはcheckout中のcommitが入るため、hashはその2行を除いて計算する。切り戻しの比較用に、`.venv/bin/python -I -m pip freeze --all` の出力もここで記録しておく(git remoteのURLを含むため、リポジトリの外に置き、共有しない)。

**不合格・未確認の場合**: Python本体の完全性、provenance、interpreterの同一性、依存関係の整合性、旧環境の保全のいずれかの確認が失敗した、または確認できない場合は、切り替えを中止する。自動で修復・再インストールをせず、旧環境を変更・削除せず、新たな明示的な承認を得るまで先へ進まない。この時点では起動方法を切り替えていないため、切り戻す作業は無く、引き続き `.venv` を使う。

**このゲートの限界**: このゲートは文書で定めた手順であり、技術的には強制されていない。`scripts/check_local_runtime.py` が確認するのは項目6・7と、Pythonのversion・仮想環境であること・editable installの向き先・command一覧と、audit hookの下での各 `--help` の表示だけで、また確認に使うscriptとlockは作業treeのものである(手順0の確認に依存する)。項目1〜5・8は人が実行して判定する。`.venv-py314-r5/bin/mtg-notion-manager` は、このゲートを通さなくても起動できてしまう。

### 切り替え後の確認項目

- `check_local_runtime.py` 成功、`.venv-py314-r5/bin/python --version` が `Python 3.14.10`。
- `doctor` が旧環境と同じ結果(認証・両DB接続・必須スキーマ)。
- dry-run・読み取り専用commandの出力(件数・分類)が旧環境と一致する。`--error-json` の出力形式(schema_version等)が変わらない。

### 停止条件

切り替えの前は上記の安全ゲートに従う。切り替えの後は、次のいずれかで、その場で中止して切り戻す: `check_local_runtime.py` の失敗、Pythonの例外(Traceback)、`doctor` の失敗、旧環境と異なるdry-run結果、想定外の確認プロンプト・終了コード、`--error-json` の形式の違い、Notionへの想定外の書き込みの兆候。いずれの場合も、自動で修復・再インストールをせず、旧環境を変更・削除しない。切り替えをやり直すには、新たな明示的な承認を得る。

## 切り戻し(rollback)

- 既存の `.venv` は作成・検証・切り替えのどの段階でも変更しないため、`.venv/bin/mtg-notion-manager` での起動に戻すだけでよい(activateしていた場合は `deactivate` してから `source .venv/bin/activate`)。PATH・shell設定・起動scriptは変更していないので、戻す設定もない。
- 設定ファイル: `.env` は移動・複製・編集しないため、復旧作業は不要。切り戻しのために `.env` を複製しない。
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
- Python本体に同梱されているpip(26.2.2)は、`setup_local_venv.py` からは実行しない。ただし環境の棚卸し時に一度だけ `python3.14 -I -m pip --version` で実行した(何もinstallしていない)。また本体を `-I` で実行すると本体directory内に `.pyc` が作られる(切り替え前の再検証の前に、archiveに含まれないものを削除するのはこのため)。

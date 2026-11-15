# Phase 3H-R7 Baseline Audit

Phase 3H-R7(Production Cutover Authorization Readiness)の開始時点の基準と、R6までの実装・証跡の棚卸し。2026-11-15に実測した値だけを記録する。

## 1. Live Git State(Phase 0、2026-11-15に個別コマンドで実測)

| 項目 | 実測値 | 判定 |
|---|---|---|
| `origin/main`(`git rev-parse origin/main`、`git ls-remote origin refs/heads/main`) | `09b651fd99295799871fc692448e27502cc3e948` | 引き継ぎSHAと一致 |
| local `main`(`git rev-parse main`) | `1c1565303283ade79d7c8ca5dbbd13dc5cb05447` | 古い。`origin/main` の祖先(`git rev-list --left-right --count main...origin/main` = `0 19`、local側の独自commitは0件)。同期は指示が無いため行っていない |
| PR #51 | `MERGED`、merge commit `09b651f`、head `0c4cccd`、`mergedAt` 2026-11-15T04:03:46Z | 引き継ぎどおり |
| main CI | run 43675088324(push、`headSha` = `09b651f`)`completed / success` | 対象SHAに紐づく |
| R6 branch `fix/phase-3h-r6-production-cutover-readiness` | local・remoteとも `0c4cccd`。`git merge-base --is-ancestor 0c4cccd origin/main` = 0、`git rev-list --count origin/main..<R6>` = 0 | mainに完全に到達済み |
| remote | `origin` = `https://github.com/daisukeokb/mtg-notion-manager.git`(fetch/push) | 想定どおり |
| 作業tree | `.claude/settings.json` のみ変更(内容は表示していない。`git diff -- .claude/settings.json \| shasum -a 256` = `6bfe5dda…`、R6時点と同じ) | 保全 |
| worktree | 1件(リポジトリ本体のみ) | — |
| 既存のR7 branch | なし | — |

R7 branch `feature/phase-3h-r7-cutover-authorization-readiness` は、`0c4cccd` と `09b651f` のtreeが同一であること(`git diff --quiet` = 0)を確認したうえで `git switch --no-track -c ... 09b651f` で作成した。切替後の `.claude/settings.json` の差分hashは同じ。

Phase 0の指示にある `git diff -- .claude/settings.json` は、内容を表示せずhashだけを取った(R6までの指示「既存の変更内容を表示する必要もない」を優先した逸脱。最終報告に記録)。

### R6 branchの扱い

- 役割: PR #51のhead。mainに完全に到達済みで、未到達commitは0件。
- 保全の必要性: Decision 07の指示「R6 post-merge verification reportが受理されるまで保持」による。受理は本R7の範囲で確認できていない。
- 通常削除の条件: 上記の受理が記録されたうえで、削除の明示指示があること。条件(到達済み・worktree非使用)は満たしている。R7では削除しない。

## 2. R6のreview済みSHAと証跡

| 項目 | 値 |
|---|---|
| R6 approved head | `0c4cccd49bebde7fc76e7811ca72c90cefc1708f` |
| R6 merge commit | `09b651fd99295799871fc692448e27502cc3e948` |
| 正式なgate実行の対象commit | `0474bbb`(`docs/evidence/phase-3h-r6/EVIDENCE.json` の `verified_commit`) |
| 正式gate実行の結果(EVIDENCE.jsonより) | Step 0・1〜8すべてPASS、rollback readiness PASS、項目4は365件中 `BYTE_IDENTICAL` 361・`MARSHAL_EQUIVALENT_APPROVED_INTERNING_DIFF` 4・`STRUCTURAL_MISMATCH` 0・`REJECTED` 0 |
| `scripts/verify_cutover_gate.py` のsha256(live tree) | `43aa71d695d1677e83f0ec56039468705b5045dec4ff03e906e25614224d49d8`(EVIDENCE.jsonの記録と一致) |
| 証跡fileのsha256(live tree) | `EVIDENCE.json` `8bba5c24…`、`gate-report-0474bbb.json` `3d36d7f1…`、`gate-report-b3d9f26.json` `fe5cd47b…` |
| 保全snapshot(R7のtest実行前に再測定) | Python本体 2278 entries(`ddf528c3…`)、`.venv-py314-r5` 5409(`6b0e6946…`)、`.venv` 3607(`51821874…`)。いずれもEVIDENCE.jsonの記録値と一致 |

R6の正式gate実行(Python本体を起動するもの)はR7では再実行していない。上記のgate結果は記録の引用であり、R7の実測値ではない。

## 3. 既存実装の責務

| 対象 | 実装 | 責務 | R7で確認したこと |
|---|---|---|---|
| Production Cutover verification | `scripts/verify_cutover_gate.py` | Cutover Safety Gate(Step 0、項目1〜8)の順序強制・fail closed・証跡のexport | 切替を技術的に強制しない(文書の「このゲートの限界」) |
| Python 3.14 bootstrap | `scripts/setup_local_venv.py` | hash固定のlockから `.venv-py314-r5` を作る | R7では実行しない |
| Runtime integrity | gate 1〜4 | archiveのsha256・release digest・attestation・展開treeと全 `.pyc` の再compile比較 | — |
| Marshal Reader | gate 4 内 | CPython 3.14.10 marshal形式のみ。実行しない | version依存(残リスク) |
| Python environment detection | `scripts/check_local_runtime.py`、gate 5〜8 | venvのversion・lock一致・`pip check`・旧環境fingerprint | — |
| CLI launcher | `pyproject.toml` の console script `mtg-notion-manager = mtg_notion_manager.cli:app` | 本番の起動経路。`-I` なしでinterpreterを起動する | **本番用launcherは存在しない**(C-2/C-3) |
| Configuration loading | `src/mtg_notion_manager/config.py` | 引数なしの `load_dotenv()`(呼び出し元moduleのdirectoryから上へ `.env` を探す)、既存の環境変数を上書きしない | 12個のcommandすべてが `Config.load()` を引数なしで呼ぶ(`cli.py`) |
| Secret handling | `.env`(リポジトリ直下、git ignore)、`.env.example` | 認証情報の唯一の保管場所 | R7では読まない |
| GitHub Actions | `.github/workflows/ci.yml`、`ci/static_guard.py` | 4 job、`contents: read`、SHA固定、Layer B | 必須checkは `test`・`lint`・`type-check`(下記G-1) |
| Test isolation | `conftest.py`(Layer A)、`ci/isolated-tests/`(Layer B) | network・`.env`・`NOTION_*` の遮断 | 子プロセスには効かない(conftestの限界記載) |
| Rollback documentation | `docs/local-python-runtime.md`「切り戻し」 | `.venv` に戻すだけ。Notionのデータは扱わない | R7で外部状態を分離して再整理 |
| Layer A / Layer B contract | `conftest.py`・`ci/isolated-tests/` | 上記 | R7は変更しない |
| Evidence collection | `docs/evidence/phase-3h-r6/` | 匿名化した正式reportの永続化 | R7の証跡も同形式で保存 |

## 4. 再利用できるtest

- `tests/test_verify_cutover_gate.py` ほか R6のgate関連test(合成archive・合成venvのfixture)。
- `tests/test_config_env_precedence.py`(shellの `NOTION_*` が `.env` より優先、`PYTHON*` がconsole scriptに届くことの合成値test)。C-2/C-3の現状の根拠。
- `tests/test_local_runtime_setup.py` の「CLIの全command一覧」test。R7のcommand一覧の整合testも同じ方法(`typer.main.get_command`)を使う。
- R7のtestは `verify_cutover_gate.py` の `PYTHON_ENVIRONMENT_KEEP`・`ARCHIVE_SHA256`・`PYTHON_VERSION` と定数が一致することを検査し、重複定義のずれを防ぐ。

## 5. 既知の未検証事項(R7開始時点)

- 第三者build(python-build-standalone)のPython本体の安全性そのもの。gate 1〜4は「attestationされたarchiveと同じもの」であることまでを示す。
- 本番の起動経路の環境変数・`.env` 探索の隔離(C-2/C-3、提案のみで未実施)。
- venvのsite-packagesのfile単位のhash(R6で「Accepted with explicit limitations」)。
- `.pth` の実行(同上)。
- GitHubのreview enforcement(G-1)。live設定はR7で再測定(`R7_GITHUB_GOVERNANCE_AUDIT.md`)。
- 本番Notionのデータの復旧手段(rollback文書の範囲外と明記されている)。
- R6開始時のsnapshot・F01の削除記録(消失。再構成しない)。

## 6. 残存リスクとの対応

`R7_RESIDUAL_RISK_REGISTER.md` に記載。リスクIDの意味は、過去のPR本文(#47〜#51)の記載から確認できたものだけを使った。**D-1は、PR #47〜#49では「CIの `setup-python` がdownloadするinterpreterと、その一回限りのpip upgradeがhash検証されない」こと(CI runnerのbootstrap)を指し、R6の文書とPR #51では「bootstrapの信頼境界」としてローカルのPython本体の文脈でも参照されている。** R7のPhase 2はローカル本番runtimeのbootstrapを扱うため、registerでは両方の範囲を分けて記載する。

## 7. R7の変更対象・非変更対象

**変更(追加のみ)**

- `scripts/cutover_readiness.py`: D-1(ローカルruntimeの範囲)のpre-launch検査、C-2/C-3の本番launcherのdry-run計画、rollbackの模擬。**無効化された部品**(実行経路なし)。
- `tests/test_cutover_readiness_bootstrap.py`・`tests/test_cutover_readiness_launcher.py`・`tests/test_cutover_readiness_rollback.py`: 合成fixtureのみ。
- `docs/phase-3h-r7/`: 本文書、runbook 2件、GitHub governance audit、residual risk register。
- `docs/evidence/phase-3h-r7/`: R7の証跡。

**変更しない**

- `src/`(CLI・`Config.load()`・Error Contract・Mutation Contract)、`pyproject.toml`(console script)、`.github/workflows/ci.yml`、`ci/`、`conftest.py`、`scripts/*.sh`、`scripts/verify_cutover_gate.py`、`requirements/`、`config/`。
- Python本体・`.venv`・`.venv-py314-r5`・本番 `.env`・GitHubの設定。

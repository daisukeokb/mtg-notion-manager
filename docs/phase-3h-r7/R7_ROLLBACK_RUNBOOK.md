# Phase 3H-R7 Rollback Runbook

**この手順書は本番の切替・切戻しの承認ではない。PRODUCTION CUTOVER NOT AUTHORIZED.** R7では本番の切戻しを実行しておらず、判断と手順は隔離した模擬環境(`scripts/cutover_readiness.py` の `assess_rollback`・`simulate_cutover`・`simulate_rollback`、`tests/test_cutover_readiness_rollback.py`)だけで検証した。

## 0. 2種類の「戻す」を混同しない

| 対象 | 戻せるか | 方法 |
|---|---|---|
| ローカルの実行環境(どのPythonでCLIを起動するか) | 戻せる(条件付き、下記1) | 起動方法を `.venv/bin/mtg-notion-manager` に戻す。`.venv`・`.env`・PATH・shell設定は切替で変更しないため、戻す設定はない |
| 外部状態(本番Notionのページ・Relation・プロパティ) | **runtimeの切戻しでは戻らない** | 切替後に実行した書き込みcommandの結果は、各commandの記録(report、`--error-json`)とNotion側の復旧手段で個別に扱う。重複統合等は削除APIを使わない設計だが、Relationの付け替え・メモ追記・タイトル更新は残る |

切替後に、書き込みの可能性があるcommand(`import`(`--dry-run` なし)、`--apply`・`--apply-schema`・`--apply-all` 付きの各command、未知のcommand)が1回でも実行されていれば、rollbackは**無条件ではない**。模擬では `external = MUTATIONS_REQUIRE_REVIEW`、`unconditional = false` になり、ローカルの切戻し自体は実行できるが「外部状態は別途人間がreviewする」と記録される。

## 1. 必要条件(Required Conditions)

| 条件 | 確認方法 | 満たさない場合 |
|---|---|---|
| 復旧対象の旧runtimeの特定 | `.venv`(CPython 3.9.6、Command Line Toolsの `/Library/Developer/CommandLineTools/usr/bin/python3` ベース)。`.venv/bin/python --version` = `Python 3.9.6` | 停止 |
| 旧runtimeの完全性 | gate 8のfingerprint(`c5e0c2be…`)と `pip freeze` のhash(`54d878d5…`)が基準値と一致。切替前に記録した値とも一致 | 停止(missing artifact / 変更あり) |
| 旧configurationとの対応 | 切替時に記録した設定fileのfingerprint(sha256)と現在の値が同じ。`.env` は切替で移動・複製・編集しない | 停止(configuration mismatch) |
| 復旧に必要なartifactの完全性 | `.venv/bin/mtg-notion-manager` が存在し、1行目が `.venv` のinterpreterを指す(gateの `rollback_readiness`) | 停止 |
| 互換性のあるデータ状態 | 切替後journalに書き込みcommandが無いこと。あれば外部状態の確認を別途行う | 条件付きのrollback |
| process停止・再開の順序 | 新runtimeで実行中のcommandが無いことを確認 → 起動方法を戻す → 旧runtimeで読み取りcommandを実行 | — |
| 復旧成功の判定 | 下記3 | 停止 |
| 復旧失敗時の停止条件 | 下記4 | — |

CLIは常駐processではない(commandごとに起動・終了する)。「停止」は、新runtimeで実行中のcommandが無く、以後新runtimeで起動しないことを意味する。

## 2. 手順

1. journalを止める(以後のcommandを新runtimeで起動しない)。実行中のcommandがあれば終了を待つ。中断しない(書き込みの途中で止めると、Mutation Contractの記録が不完全になる)。
2. 上記1の必要条件を確認する(`assess_rollback` と同じ判断)。1つでも満たさなければ止める。
3. 起動方法を `.venv/bin/mtg-notion-manager`(またはlauncherの計画で旧interpreterを指定)に戻す。activateしていた場合は `deactivate` してから `source .venv/bin/activate`。
4. 下記3の判定を行う。
5. journalに書き込みcommandがあった場合は、その一覧(command名・日時・終了コード・report)を人間に渡し、Notion側の確認を依頼する。runtimeの切戻しで解決したことにしない。

## 3. 復旧成功の判定

- `.venv/bin/python --version` が `Python 3.9.6`。
- gate 8の2つのhashが基準値と一致(`.venv` が変更されていない)。
- `.venv/bin/mtg-notion-manager --help` が表示される。
- `doctor` が切替前と同じ結果(これは本番Notionの読み取りであり、切戻しの承認の範囲で行う)。

## 4. 停止条件(復旧失敗時)

- 必要条件のいずれかが満たされない。
- 切戻しの途中で失敗した(模擬では、切替先を書き換える前に失敗した場合、状態は「新runtimeのまま」で一貫し、半端な状態にならないことを確認した)。
- 切戻し後のhealth checkが失敗した。

いずれも、自動の修復・再インストール・`.venv` の作り直しをしない。旧環境を変更・削除しない。状態と証跡を記録して止まり、新たな明示的な承認を待つ。

## 5. 模擬環境での検証(R7で実施)

模擬環境は一時directoryの中の `legacy/`・`new/` と、どちらで起動するかを表すselector file。実際の切替にselectorは存在しない(起動pathを人が選ぶ)。模擬は判断と順序の論理を検証するもので、実runtimeでの切戻しの代わりにはならない。

| シナリオ | 期待 | test |
|---|---|---|
| 切替前状態への復帰 | 新runtimeの起動失敗 → abort → 旧runtime、`legacy/` の内容が切替前と同一、作業fileが残らない | `test_new_runtime_start_failure_rolls_back_to_the_pre_cutover_state` |
| 新runtimeの起動失敗 | 同上(`ROLLED_BACK`) | 同上 |
| configuration mismatch | 切戻しを止め、新runtimeのまま(FAIL) | `test_configuration_mismatch_stops_rollback`、`test_missing_config_is_a_mismatch` |
| missing artifact | 同上 | `test_missing_artifact_stops_rollback`、`test_changed_legacy_runtime_stops_rollback` |
| rollback途中失敗 | selectorは書き換え前の値で一貫(atomicな置換)、一時fileなし、自動修復なし。後の再試行は成功 | `test_failure_during_rollback_leaves_a_consistent_selector` |
| rollback後のhealth check失敗 | FAIL、停止 | `test_health_check_failure_after_rollback`、`test_new_runtime_failure_and_legacy_failure` |
| 非可逆データ変更の検出 | 書き込みの可能性があるcommandがjournalにあれば `unconditional = false`(ローカルの切戻しは可能) | `test_irreversible_mutation_makes_rollback_conditional`、`test_read_only_journal_is_unconditional` |

書き込みの可能性の判定(`command_may_mutate`)は、CLIの実際のoptionと照合している(読み取り専用commandに `--apply*` optionが無いことをtestで確認)。未知のcommandは書き込みの可能性ありとして扱う。

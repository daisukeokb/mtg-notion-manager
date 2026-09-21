"""emit_error_json()のError Contract v1互換性 / v2(mutation)拡張のunit tests。

CLIコマンドは経由しない(error_contract.pyとmutation_contract.pyのserialization
契約だけを直接検証する)。既存8コマンドのCLI経由のtestはtest_cli_error_contract.pyに
既にあるため、ここでは重複させず、emit_error_json()そのものの直接呼び出しに限定する。
"""

from __future__ import annotations

import json

import pytest

from mtg_notion_manager.error_contract import SCHEMA_VERSION, SCHEMA_VERSION_V2, emit_error_json
from mtg_notion_manager.mutation_contract import (
    MutationOperation,
    MutationSummary,
    RecoveryAction,
)

# --- v1 compatibility --------------------------------------------------------------


def test_v1_output_omits_mutation_key(capsys: pytest.CaptureFixture[str]) -> None:
    emit_error_json("import-article", "MAPPING", "UNMAPPED_VALUE", "セット名が不明です。")

    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 1
    assert "mutation" not in payload


def test_v1_output_is_byte_for_byte_unchanged_by_the_v2_extension(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mutationパラメータを一切渡さない既存呼び出し形は、v2拡張の追加前後で
    出力が完全に同一であることを固定する(golden equality)。"""
    emit_error_json("doctor", "CONFIGURATION", "CONFIG_LOAD_FAILED", "NOTION_API_KEY が未設定です")

    actual = capsys.readouterr().out
    expected = (
        json.dumps(
            {
                "schema_version": 1,
                "command": "doctor",
                "error_category": "CONFIGURATION",
                "error_code": "CONFIG_LOAD_FAILED",
                "message": "NOTION_API_KEY が未設定です",
            },
            ensure_ascii=False,
        )
        + "\n"
    )

    assert actual == expected
    assert SCHEMA_VERSION == 1  # このgoldenが前提とする定数自体も変化していないことを確認


def test_omitting_mutation_keyword_entirely_still_works(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mutation引数を一切渡さない既存の位置引数呼び出しが、シグネチャ拡張後も
    そのまま動作することを確認する(既存8コマンドのcall siteは無変更で済む)。"""
    emit_error_json("verify-import", "PRODUCTION_API", "NOTION_API_ERROR", "timeout")

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert "mutation" not in payload


# --- v2 serialization ----------------------------------------------------------------


def test_v2_output_includes_schema_version_2_and_mutation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutation = MutationSummary(
        attempted=3,
        succeeded=2,
        failed=1,
        unknown=0,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
    )

    emit_error_json(
        "import-cards",
        "PRODUCTION_API",
        "NOTION_API_ERROR",
        "10件中2件成功、1件失敗しました。",
        mutation=mutation,
    )

    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == SCHEMA_VERSION_V2
    assert payload["schema_version"] == 2
    assert payload["command"] == "import-cards"
    assert payload["error_category"] == "PRODUCTION_API"
    assert payload["error_code"] == "NOTION_API_ERROR"
    assert payload["mutation"] == {
        "state": "PARTIAL_MUTATION",
        "attempted": 3,
        "succeeded": 2,
        "failed": 1,
        "unknown": 0,
        "recovery_action": "RECONCILE_BEFORE_RETRY",
    }


def test_v2_output_is_a_single_pure_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    mutation = MutationSummary(
        attempted=1,
        succeeded=0,
        failed=0,
        unknown=1,
        recovery_action=RecoveryAction.MANUAL_REVIEW_REQUIRED,
        operations=(MutationOperation(key="島", action="create", state="unknown"),),
    )

    emit_error_json(
        "import-cards", "PRODUCTION_API", "NOTION_API_ERROR", "timeout", mutation=mutation
    )

    out = capsys.readouterr().out
    assert "\x1b" not in out
    payload = json.loads(out)  # stdout全体が単一のJSONオブジェクトとしてparse可能であること
    assert payload["mutation"]["operations"] == [
        {"key": "島", "action": "create", "state": "unknown"}
    ]


def test_caller_cannot_directly_control_schema_version() -> None:
    """emit_error_json()にschema_versionを直接指定できるキーワードは存在しない
    (mutationの有無だけがversionを決める)。"""
    import inspect

    signature = inspect.signature(emit_error_json)
    assert "schema_version" not in signature.parameters

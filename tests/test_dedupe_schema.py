from __future__ import annotations

import httpx
import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services.dedupe_schema import (
    SchemaMigrationExecutionError,
    SchemaMigrationResult,
    SchemaWriteCompletion,
    execute_schema_migration,
)

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class _FakeSchemaClient:
    """update_data_source_schema()だけを模すfake NotionClient(schema専用)。

    DedupeRepository.apply_schema_migration()の実装(notion/dedupe_repository.py)
    を実際に通す――execute_schema_migration()単体をmockするのではなく、
    real DedupeRepositoryを経由することで、単一PATCH契約(1回のNotion呼び出し
    にまとめる)が壊れていないことも間接的に検証する。
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.schema_update_calls: list[tuple[str, dict]] = []

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        self.schema_update_calls.append((data_source_id, dict(properties)))
        if self._error is not None:
            raise self._error
        return {"properties": properties}


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}")
    response = httpx.Response(400, request=request)
    return httpx.HTTPStatusError("bad request", request=request, response=response)


def test_t1_schema_success_returns_structured_result() -> None:
    client = _FakeSchemaClient()
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    result = execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert isinstance(result, SchemaMigrationResult)
    assert result.completion == SchemaWriteCompletion.SUCCEEDED
    assert result.property_names == ("所持枚数", "統合済み")
    # single-PATCH契約: 複数プロパティでもNotion呼び出しは1回だけ。
    assert len(client.schema_update_calls) == 1


def test_t2_known_failure_from_http_status_error_cause() -> None:
    cause = _http_status_error()
    notion_error = NotionAPIError("Notion API呼び出しに失敗しました (400): bad request")
    notion_error.__cause__ = cause
    client = _FakeSchemaClient(error=notion_error)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数"])

    carrier = exc_info.value
    assert carrier.result.completion == SchemaWriteCompletion.KNOWN_FAILED
    assert carrier.result.property_names == ("所持枚数",)
    # human-visible messageは元のNotionAPIErrorのものをそのまま維持する。
    assert str(carrier) == str(notion_error)
    assert carrier.__cause__ is notion_error


def test_t3_timeout_is_unknown() -> None:
    cause = httpx.TimeoutException("timed out")
    notion_error = NotionAPIError("Notion APIへの接続がタイムアウトしました: timed out")
    notion_error.__cause__ = cause
    client = _FakeSchemaClient(error=notion_error)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN


def test_t4_cause_less_error_is_unknown() -> None:
    """causeが一切ない(想定外の)NotionAPIErrorも安全側のUNKNOWNへ倒す。"""
    notion_error = NotionAPIError("Notion APIへの接続に失敗しました: connection reset")
    client = _FakeSchemaClient(error=notion_error)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN


def test_t5_classification_is_message_independent_both_directions() -> None:
    """分類はexc.__cause__の型だけで決まり、messageの文言には一切依存しない。"""
    # message文字列は"timeout"を含むが、実際のcauseはHTTPStatusError → KNOWN_FAILED。
    http_cause = _http_status_error()
    misleading_message_error = NotionAPIError(
        "timeout-looking message but the actual cause is an HTTP status error"
    )
    misleading_message_error.__cause__ = http_cause
    client_a = _FakeSchemaClient(error=misleading_message_error)
    repo_a = DedupeRepository(client_a, DATA_SOURCE_ID)
    with pytest.raises(SchemaMigrationExecutionError) as exc_info_a:
        execute_schema_migration(repo_a, ["所持枚数"])
    assert exc_info_a.value.result.completion == SchemaWriteCompletion.KNOWN_FAILED

    # message文字列は確定的な失敗に見えるが、実際のcauseはTimeoutException → UNKNOWN。
    timeout_cause = httpx.TimeoutException("timed out")
    definitive_looking_error = NotionAPIError(
        "Notion API呼び出しに失敗しました (400): this looks definitively failed"
    )
    definitive_looking_error.__cause__ = timeout_cause
    client_b = _FakeSchemaClient(error=definitive_looking_error)
    repo_b = DedupeRepository(client_b, DATA_SOURCE_ID)
    with pytest.raises(SchemaMigrationExecutionError) as exc_info_b:
        execute_schema_migration(repo_b, ["所持枚数"])
    assert exc_info_b.value.result.completion == SchemaWriteCompletion.UNKNOWN


def test_t6_exception_chain_preserved() -> None:
    http_cause = _http_status_error()
    notion_error = NotionAPIError("Notion API呼び出しに失敗しました (400): bad request")
    notion_error.__cause__ = http_cause
    client = _FakeSchemaClient(error=notion_error)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数"])

    carrier = exc_info.value
    assert carrier.__cause__ is notion_error
    assert notion_error.__cause__ is http_cause

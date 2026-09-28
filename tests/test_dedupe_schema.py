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
    """update_data_source_schema() / get_data_source() を模すfake NotionClient
    (schema専用)。

    DedupeRepository.apply_schema_migration()/get_schema()の実装
    (notion/dedupe_repository.py)を実際に通す――execute_schema_migration()単体を
    mockするのではなく、real DedupeRepositoryを経由することで、単一PATCH契約
    (1回のNotion呼び出しにまとめる)が壊れていないことも間接的に検証する。

    verification_schema省略時は`{"properties": {}}`(=対象propertyが1件も
    存在しない、Phase 3Bのpositive-only verificationでは常にinconclusive)を
    返す。UNKNOWN既存回帰テスト(T3/T4/T5)が新しいverification呼び出しに
    よって意図せずSUCCEEDEDへreconcileされてしまわないよう、明示的に
    「verification inconclusive」側をdefaultにしている(§27の指示通り、
    テストを弱めるのではなくfixtureをverification inconclusiveとして
    明示化する対応)。
    """

    def __init__(
        self,
        *,
        error: Exception | None = None,
        verification_schema: dict | None = None,
        verification_error: Exception | None = None,
    ) -> None:
        self._error = error
        self._verification_schema = (
            verification_schema if verification_schema is not None else {"properties": {}}
        )
        self._verification_error = verification_error
        self.schema_update_calls: list[tuple[str, dict]] = []
        self.get_data_source_calls: list[str] = []

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        self.schema_update_calls.append((data_source_id, dict(properties)))
        if self._error is not None:
            raise self._error
        return {"properties": properties}

    def get_data_source(self, data_source_id: str) -> dict:
        self.get_data_source_calls.append(data_source_id)
        if self._verification_error is not None:
            raise self._verification_error
        return self._verification_schema


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
    # 直接成功(PATCHが例外を送出しない)した場合、verification read-backは
    # 一切試みられない(B-T1)。
    assert client.get_data_source_calls == []


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
    # KNOWN_FAILEDはread-backによって覆さない方針のため、verificationは
    # 一切試みられない(B-T2)。
    assert client.get_data_source_calls == []


def test_t3_timeout_is_unknown() -> None:
    """verification_schema省略(=対象propertyなし)によりverificationはinconclusive
    となり、Phase 2O以来の既存contract(UNKNOWNのまま)を維持する(B-T4相当)。"""
    cause = httpx.TimeoutException("timed out")
    notion_error = NotionAPIError("Notion APIへの接続がタイムアウトしました: timed out")
    notion_error.__cause__ = cause
    client = _FakeSchemaClient(error=notion_error)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN
    # UNKNOWNの場合のみverificationが1回試みられる。
    assert len(client.get_data_source_calls) == 1


def test_t4_cause_less_error_is_unknown() -> None:
    """causeが一切ない(想定外の)NotionAPIErrorも安全側のUNKNOWNへ倒す。"""
    notion_error = NotionAPIError("Notion APIへの接続に失敗しました: connection reset")
    client = _FakeSchemaClient(error=notion_error)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN
    assert len(client.get_data_source_calls) == 1


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
    assert client_a.get_data_source_calls == []

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
    assert len(client_b.get_data_source_calls) == 1


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
    assert client.get_data_source_calls == []


# --- Phase 3B: UNKNOWN completion専用のpositive-only post-write verification ---


def _positive_verification_schema(*, extra_properties: dict | None = None) -> dict:
    """SCHEMA_ADDITIONSが期待する型と完全一致するschema propertiesを組み立てる。"""
    properties = {
        "所持枚数": {
            "id": "prop-quantity",
            "name": "所持枚数",
            "type": "number",
            "number": {"format": "number"},
        },
        "統合済み": {
            "id": "prop-merged",
            "name": "統合済み",
            "type": "checkbox",
            "checkbox": {},
        },
    }
    if extra_properties:
        properties.update(extra_properties)
    return {"properties": properties}


def _unknown_error(
    message: str = "Notion APIへの接続がタイムアウトしました: timed out",
) -> NotionAPIError:
    error = NotionAPIError(message)
    error.__cause__ = httpx.TimeoutException("timed out")
    return error


def test_b_t3_unknown_with_full_positive_match_reconciles_to_success() -> None:
    client = _FakeSchemaClient(
        error=_unknown_error(), verification_schema=_positive_verification_schema()
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    result = execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert isinstance(result, SchemaMigrationResult)
    assert result.completion == SchemaWriteCompletion.SUCCEEDED
    assert result.property_names == ("所持枚数", "統合済み")
    assert len(client.schema_update_calls) == 1  # single-PATCH契約は維持。
    assert len(client.get_data_source_calls) == 1


def test_b_t4_unknown_with_no_properties_found_remains_unknown() -> None:
    client = _FakeSchemaClient(
        error=_unknown_error(), verification_schema={"properties": {}}
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN
    assert len(client.get_data_source_calls) == 1


def test_b_t5_unknown_with_partial_properties_remains_unknown() -> None:
    partial_schema = {
        "properties": {
            "所持枚数": {"type": "number", "number": {"format": "number"}},
            # 統合済みは欠落させる。
        }
    }
    client = _FakeSchemaClient(error=_unknown_error(), verification_schema=partial_schema)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN
    assert len(client.get_data_source_calls) == 1


def test_b_t6_unknown_with_type_mismatch_remains_unknown() -> None:
    mismatched_schema = {
        "properties": {
            # 所持枚数という名前は存在するが、typeがnumberではない。
            "所持枚数": {"type": "rich_text", "rich_text": {}},
            "統合済み": {"type": "checkbox", "checkbox": {}},
        }
    }
    client = _FakeSchemaClient(error=_unknown_error(), verification_schema=mismatched_schema)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN


def test_b_t7_unknown_positive_match_ignores_unrelated_properties() -> None:
    """full schema equalityではなくsubset一致であることを固定する。

    無関係なproperty(販売価格)が新たに存在・変更されていても、要求された
    property/typeさえ一致していればpositive verificationは成功する。"""
    schema = _positive_verification_schema(
        extra_properties={"販売価格": {"type": "number", "number": {"format": "yen"}}}
    )
    client = _FakeSchemaClient(error=_unknown_error(), verification_schema=schema)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    result = execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert result.completion == SchemaWriteCompletion.SUCCEEDED


def test_b_t8_verification_read_failure_preserves_unknown() -> None:
    """verification GET自体が失敗しても、originalのUNKNOWNをKNOWN_FAILEDへ
    変換したり、新しいmutation failureとして扱ったりしない。"""
    verification_failure = NotionAPIError(
        "Notion APIへの接続がタイムアウトしました: verify timed out"
    )
    verification_failure.__cause__ = httpx.TimeoutException("verify timed out")
    client = _FakeSchemaClient(error=_unknown_error(), verification_error=verification_failure)
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数", "統合済み"])

    carrier = exc_info.value
    assert carrier.result.completion == SchemaWriteCompletion.UNKNOWN
    # original UNKNOWNの例外チェインはverification GETの失敗によって
    # 上書きされない(元のPATCH timeoutがそのまま__cause__)。
    original_patch_error = carrier.__cause__
    assert isinstance(original_patch_error, NotionAPIError)
    assert isinstance(original_patch_error.__cause__, httpx.TimeoutException)
    assert "timed out" in str(original_patch_error)


def test_b_t13_verification_checks_requested_subset_only() -> None:
    """verification対象はSchemaMigrationResult.property_names(今回要求した分)
    だけであり、既に存在した無関係propertyまで比較対象にしない。"""
    client = _FakeSchemaClient(
        error=_unknown_error(),
        verification_schema=_positive_verification_schema(),
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    # 所持枚数だけを要求 → 統合済みの状態は一切参照されないはず。
    result = execute_schema_migration(repo, ["所持枚数"])

    assert result.completion == SchemaWriteCompletion.SUCCEEDED
    assert result.property_names == ("所持枚数",)


def test_b_t14_positive_verification_is_message_independent() -> None:
    """positive verificationの成否はexc.__cause__の型(UNKNOWN判定)と
    read-back結果だけで決まり、例外messageの文言には一切依存しない。"""
    misleading_error = NotionAPIError(
        "この文言は成功しているように見えるが実際はUNKNOWN分類である"
    )
    misleading_error.__cause__ = httpx.TimeoutException("timed out")
    client = _FakeSchemaClient(
        error=misleading_error, verification_schema=_positive_verification_schema()
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    result = execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert result.completion == SchemaWriteCompletion.SUCCEEDED

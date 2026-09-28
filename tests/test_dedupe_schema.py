from __future__ import annotations

import httpx
import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services.dedupe_schema import (
    SchemaMigrationExecutionError,
    SchemaMigrationResult,
    SchemaVerificationState,
    SchemaWriteCompletion,
    execute_schema_migration,
    schema_prerequisite_satisfied,
    verify_schema_properties_present,
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


class _MalformedSchemaClient:
    """update_data_source_schema()はUNKNOWN相当の例外を送出し、
    get_data_source()は与えられた任意の(不正形状を含む)値をそのまま返す
    fake低レベルNotionClient(R1-T9〜T13のmalformed read-back専用)。

    _FakeSchemaClientの「Noneなら既定値{"properties": {}}を使う」という
    利便性を持たない――malformed-shapeテストではresponse自体がNoneである
    ケースも明示的に検証したいため、常に与えられた値をそのまま返す。
    """

    def __init__(self, *, patch_error: Exception, verification_response: object) -> None:
        self._patch_error = patch_error
        self._verification_response = verification_response
        self.get_data_source_calls: list[str] = []

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        raise self._patch_error

    def get_data_source(self, data_source_id: str) -> object:
        self.get_data_source_calls.append(data_source_id)
        return self._verification_response


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
    # R1-T1: direct successはverification=NOT_ATTEMPTED(検証を試みていない)。
    assert result.verification == SchemaVerificationState.NOT_ATTEMPTED
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
    # R1-T2: KNOWN_FAILEDはverificationを一切試みない(NOT_ATTEMPTED)。
    assert carrier.result.verification == SchemaVerificationState.NOT_ATTEMPTED
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
    """Phase 3B-R1: completionはUNKNOWNのまま(SUCCEEDEDへ書き換えない)、
    verification=DESIRED_STATE_VERIFIEDとして正常returnする(R1-T3)。"""
    client = _FakeSchemaClient(
        error=_unknown_error(), verification_schema=_positive_verification_schema()
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    result = execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert isinstance(result, SchemaMigrationResult)
    assert result.completion == SchemaWriteCompletion.UNKNOWN
    assert result.completion != SchemaWriteCompletion.SUCCEEDED
    assert result.verification == SchemaVerificationState.DESIRED_STATE_VERIFIED
    assert result.property_names == ("所持枚数", "統合済み")
    assert len(client.schema_update_calls) == 1  # single-PATCH契約は維持。
    assert len(client.get_data_source_calls) == 1
    # write outcomeがUNKNOWNのままでも、dedupe phaseへ進んでよいと判定される。
    assert schema_prerequisite_satisfied(result) is True


def test_b_t4_unknown_with_no_properties_found_remains_unknown() -> None:
    client = _FakeSchemaClient(
        error=_unknown_error(), verification_schema={"properties": {}}
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数", "統合済み"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN
    assert exc_info.value.result.verification == SchemaVerificationState.INCONCLUSIVE
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
    assert exc_info.value.result.verification == SchemaVerificationState.INCONCLUSIVE
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
    assert exc_info.value.result.verification == SchemaVerificationState.INCONCLUSIVE


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

    assert result.completion == SchemaWriteCompletion.UNKNOWN
    assert result.verification == SchemaVerificationState.DESIRED_STATE_VERIFIED


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
    assert carrier.result.verification == SchemaVerificationState.INCONCLUSIVE
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

    assert result.completion == SchemaWriteCompletion.UNKNOWN
    assert result.verification == SchemaVerificationState.DESIRED_STATE_VERIFIED
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

    assert result.completion == SchemaWriteCompletion.UNKNOWN
    assert result.verification == SchemaVerificationState.DESIRED_STATE_VERIFIED


# --- Phase 3B-R1: malformed read-back must never mask original UNKNOWN ------
#
# Phase 3B-R0監査で、verify_schema_properties_present()がmalformed response
# shapeに対して未捕捉のAttributeErrorを送出し、original UNKNOWNをmaskして
# raw tracebackでcommandを終了させることが実証された(fail-closed gap)。
# ここではその修正(shape validation + 最終安全境界)を固定する。


@pytest.mark.parametrize(
    "label,verification_response",
    [
        ("empty_dict", {}),
        ("properties_null", {"properties": None}),
        ("properties_list", {"properties": []}),
        ("properties_string", {"properties": "not-a-dict"}),
        ("response_is_none", None),
        ("response_is_string", "unexpected scalar response"),
        ("response_is_number", 42),
        ("property_entry_null", {"properties": {"所持枚数": None, "統合済み": None}}),
        ("property_entry_list", {"properties": {"所持枚数": [], "統合済み": []}}),
        (
            "property_entry_string",
            {"properties": {"所持枚数": "invalid", "統合済み": "invalid"}},
        ),
        (
            "property_entry_missing_type",
            {"properties": {"所持枚数": {"id": "x"}, "統合済み": {"id": "y"}}},
        ),
        (
            "type_is_null",
            {
                "properties": {
                    "所持枚数": {"type": None, "number": {}},
                    "統合済み": {"type": None, "checkbox": {}},
                }
            },
        ),
        (
            "type_is_wrong_type",
            {
                "properties": {
                    "所持枚数": {"type": 123, "number": {}},
                    "統合済み": {"type": 123, "checkbox": {}},
                }
            },
        ),
    ],
)
def test_r1_t9_to_t13_malformed_read_back_preserves_original_unknown(
    label: str, verification_response: object
) -> None:
    """R1-T9〜T13: いずれのmalformed/unexpected response shapeでも、raw
    exceptionを送出せずinconclusiveとして扱い、original UNKNOWNを維持する。
    """
    client = _MalformedSchemaClient(
        patch_error=_unknown_error(), verification_response=verification_response
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo, ["所持枚数", "統合済み"])

    carrier = exc_info.value
    assert carrier.result.completion == SchemaWriteCompletion.UNKNOWN, label
    assert carrier.result.verification == SchemaVerificationState.INCONCLUSIVE, label
    assert len(client.get_data_source_calls) == 1, label


def test_r1_t14_verification_helper_unexpected_exception_preserves_unknown() -> None:
    """verify_schema_properties_present()の内部処理が想定外の例外
    (AttributeError/TypeError/ValueError等)を送出しても、execute_schema_migration()
    まで伝播させず、original UNKNOWNを維持する(最終安全境界の直接テスト)。
    """

    class _ExplodingDict(dict):
        def get(self, *args: object, **kwargs: object) -> object:
            raise TypeError("simulated unexpected parsing failure")

    client = _MalformedSchemaClient(
        patch_error=_unknown_error(), verification_response=_ExplodingDict({"properties": {}})
    )
    repo = DedupeRepository(client, DATA_SOURCE_ID)

    # verify_schema_properties_present()を直接呼んでも例外を外へ漏らさない。
    assert verify_schema_properties_present(repo, ("所持枚数",)) is False

    # execute_schema_migration()経由でもoriginal UNKNOWNが維持される。
    client2 = _MalformedSchemaClient(
        patch_error=_unknown_error(), verification_response=_ExplodingDict({"properties": {}})
    )
    repo2 = DedupeRepository(client2, DATA_SOURCE_ID)
    with pytest.raises(SchemaMigrationExecutionError) as exc_info:
        execute_schema_migration(repo2, ["所持枚数"])

    assert exc_info.value.result.completion == SchemaWriteCompletion.UNKNOWN
    assert exc_info.value.result.verification == SchemaVerificationState.INCONCLUSIVE


def test_r1_shape_validation_does_not_swallow_base_exceptions() -> None:
    """KeyboardInterrupt/SystemExit等のBaseExceptionはverificationの安全境界
    (except Exception)で捕捉されず、そのまま伝播する(§20の要求通り)。"""

    class _InterruptingClient:
        def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
            raise _unknown_error()

        def get_data_source(self, data_source_id: str) -> dict:
            raise KeyboardInterrupt()

    repo = DedupeRepository(_InterruptingClient(), DATA_SOURCE_ID)

    with pytest.raises(KeyboardInterrupt):
        execute_schema_migration(repo, ["所持枚数"])

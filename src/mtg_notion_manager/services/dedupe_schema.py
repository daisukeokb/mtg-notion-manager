"""MTGカードDBのスキーマ変更(dedupe-cards --apply-schema)実行結果を
structuredに保持するための最小限のfactual model。

責務の分離: このmoduleはschema write実行の事実(SUCCEEDED/KNOWN_FAILED/
UNKNOWN)だけを保持する。Error Contract表現(MutationState/RecoveryAction/
MutationOperation等、mutation_contract.py)への変換は将来のadapterの責務で
あり、このmoduleはmutation_contract.py/error_contract.pyへ一切依存しない。

single-PATCH契約(DedupeRepository.apply_schema_migration()は不足プロパティ
全てを1回のPATCHへまとめて送信する、notion/dedupe_repository.py参照)は
変更しない。そのためschema write試行数は常に0か1であり、property単位の
success/failureという粒度の情報はNotion API自体から得られない(1 PATCH
全体がsucceed/failするのみ)。したがってこのmoduleはproperty単位の
failed_property相当を一切主張しない。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from mtg_notion_manager.exceptions import NotionAPIError, SchemaMigrationError
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository


class SchemaWriteCompletion:
    """schema migration write(1回のPATCH)が実際にどう完了したかについて
    わかっている事実。NotionAPIError.__cause__ の型だけから判定する
    (str(exc)のメッセージ文字列は一切見ない)。dedupe write側の
    GroupWriteCompletion(services/dedupe_cards.py)と同じ安全方針:
    サーバーが明示的なHTTPエラー応答を返した場合だけをKNOWN_FAILEDとし、
    それ以外(タイムアウト・cause不明・その他の接続エラー)はすべて
    安全側のUNKNOWNへ倒す。
    """

    SUCCEEDED = "succeeded"
    KNOWN_FAILED = "known_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SchemaMigrationResult:
    """schema write(1回のPATCH)の実行結果。property_namesはこの1回の
    PATCHに含まれていたプロパティ名の一覧であり、property単位の
    success/failure情報ではない(1 PATCH全体の結果を表すだけ)。
    """

    completion: str
    property_names: tuple[str, ...]


class SchemaMigrationExecutionError(SchemaMigrationError):
    """schema write(1回のPATCH)が失敗した、または完了状態が確定できない。

    result.completion(KNOWN_FAILED/UNKNOWN)へ、実際に試みられたプロパティ名と
    ともに構造化された事実を保持する。呼び出し元がstr(exc)で受け取る
    human-visible messageは、元のNotionAPIErrorのメッセージをそのまま
    使う(Error Contractはまだ接続しないため、既存のCLI表示を変更しない)。

    例外チェイン: SchemaMigrationExecutionError.__cause__ は常に元の
    NotionAPIError(`raise ... from exc`で設定)であり、そのNotionAPIError
    自体の__cause__にはhttpxレベルの例外(HTTPStatusError/TimeoutException等)
    が保持されたまま失われない。
    """

    def __init__(self, result: SchemaMigrationResult, message: str) -> None:
        super().__init__(message)
        self.result = result


def _completion_from_notion_api_error(exc: NotionAPIError) -> str:
    if isinstance(exc.__cause__, httpx.HTTPStatusError):
        return SchemaWriteCompletion.KNOWN_FAILED
    return SchemaWriteCompletion.UNKNOWN


def execute_schema_migration(
    repo: DedupeRepository, property_names: list[str]
) -> SchemaMigrationResult:
    """schema migration(1回のPATCH)を実行し、結果を構造化して返す。

    成功時はSchemaMigrationResult(completion=SUCCEEDED)を返す。失敗時は
    DedupeRepository.apply_schema_migration()が送出するNotionAPIErrorの
    __cause__の型だけ(str(exc)のメッセージは一切見ない)からKNOWN_FAILED/
    UNKNOWNを判定し、SchemaMigrationExecutionErrorとして再送出する。

    schema failureは引き続き例外として呼び出し元へ伝播する(control flowを
    変更しない――呼び出し元の単一tryブロックが、schema失敗時にdedupe phase
    へ進まないという既存のfail-closed挙動を構造的に維持する)。
    """
    try:
        repo.apply_schema_migration(property_names)
    except NotionAPIError as exc:
        completion = _completion_from_notion_api_error(exc)
        raise SchemaMigrationExecutionError(
            SchemaMigrationResult(completion=completion, property_names=tuple(property_names)),
            str(exc),
        ) from exc
    return SchemaMigrationResult(
        completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(property_names)
    )

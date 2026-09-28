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

Phase 3B: UNKNOWN completion専用のpositive-only post-write verification。
KNOWN_FAILEDをread-backによって覆すことは一切しない(read-backするのは
completion==UNKNOWNの場合のみ)。verify_schema_properties_present()は、
要求されたproperty全てが期待するtypeで現在のdata source schemaに存在する
場合にのみTrueを返す(subset一致。無関係なpropertyの有無は無視する)。
1件でも欠落・型不一致・確認不能(read failure含む)ならFalseを返し、
呼び出し元はUNKNOWNをそのまま維持すること。

この検証が証明するのは「今回のPATCH request自体が成功した」ことではなく、
「dedupe実行に必要なdesired schema stateが現在のNotion上で成立している」
ことだけである(別actorが同じschemaを用意した可能性を理論上排除できない
ため)。read-after-write consistencyやPATCHのmulti-property atomicityは
Notion公式ドキュメントで保証が確認できなかった(Phase 3A監査時点)ため、
verificationは1回のGETのみに限定し、polling・リトライは一切行わない。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from mtg_notion_manager.exceptions import NotionAPIError, SchemaMigrationError
from mtg_notion_manager.notion.dedupe_repository import SCHEMA_ADDITIONS, DedupeRepository


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


def verify_schema_properties_present(
    repo: DedupeRepository, property_names: tuple[str, ...]
) -> bool:
    """UNKNOWN completion後のpositive-only read-back verification(1回のGETのみ)。

    property_names の各名前が、現在のdata source schemaに
    SCHEMA_ADDITIONSで定義された型(number/checkbox)で存在する場合にのみ
    Trueを返す(要求されたsubsetだけを確認する――無関係なpropertyの追加・
    変更は無視する。full schema equalityは要求しない)。

    以下は全てFalse(=inconclusive、呼び出し元はUNKNOWNを維持すること):
    - 1件でもpropertyが存在しない
    - 1件でもtypeが期待と異なる
    - SCHEMA_ADDITIONSに定義のない名前が渡された(通常発生しない防御)
    - read-back自体がNotionAPIErrorで失敗した(timeout/HTTPエラー問わず、
      安全側に倒しFalseを返す――read failureを新たなmutation failureとして
      扱ったり、original UNKNOWNをKNOWN_FAILEDへ変換したりしない)

    read-backは1回のみ試行する(pollingやリトライは行わない)。
    """
    try:
        schema_properties = repo.get_schema().get("properties", {})
    except NotionAPIError:
        return False

    for name in property_names:
        expected_definition = SCHEMA_ADDITIONS.get(name)
        if expected_definition is None:
            return False
        expected_type = next(iter(expected_definition))
        actual_property = schema_properties.get(name)
        if actual_property is None or actual_property.get("type") != expected_type:
            return False

    return True


def execute_schema_migration(
    repo: DedupeRepository, property_names: list[str]
) -> SchemaMigrationResult:
    """schema migration(1回のPATCH)を実行し、結果を構造化して返す。

    成功時はSchemaMigrationResult(completion=SUCCEEDED)を返す。失敗時は
    DedupeRepository.apply_schema_migration()が送出するNotionAPIErrorの
    __cause__の型だけ(str(exc)のメッセージは一切見ない)からKNOWN_FAILED/
    UNKNOWNを判定する。

    completionがUNKNOWNの場合に限り、verify_schema_properties_present()で
    positive-only read-back verificationを1回だけ試みる。要求された
    property全てが期待するtypeで確認できた場合は、「dedupe実行に必要な
    desired schema stateが現在成立している」とみなしSUCCEEDEDへreconcileし
    (通常の成功時と同じSchemaMigrationResultを返す――今回のPATCH自体が
    成功したことの証明ではない点に注意)、既存の成功pathへ自然に合流させる。
    確認できなかった場合(欠落・型不一致・read failure等)は、KNOWN_FAILEDの
    場合と同様にSchemaMigrationExecutionErrorとして再送出する。

    schema failureは引き続き例外として呼び出し元へ伝播する(control flowを
    変更しない――呼び出し元の単一tryブロックが、schema失敗時にdedupe phase
    へ進まないという既存のfail-closed挙動を構造的に維持する)。
    """
    try:
        repo.apply_schema_migration(property_names)
    except NotionAPIError as exc:
        completion = _completion_from_notion_api_error(exc)
        if completion == SchemaWriteCompletion.UNKNOWN and verify_schema_properties_present(
            repo, tuple(property_names)
        ):
            return SchemaMigrationResult(
                completion=SchemaWriteCompletion.SUCCEEDED,
                property_names=tuple(property_names),
            )
        raise SchemaMigrationExecutionError(
            SchemaMigrationResult(completion=completion, property_names=tuple(property_names)),
            str(exc),
        ) from exc
    return SchemaMigrationResult(
        completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(property_names)
    )

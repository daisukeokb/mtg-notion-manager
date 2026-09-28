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
1件でも欠落・型不一致・確認不能(read failure・想定外のresponse形状含む)
ならFalseを返し、呼び出し元はUNKNOWNをそのまま維持すること。

Phase 3B-R1: この検証が証明するのは「今回のPATCH request自体が成功した」
ことではなく、「dedupe実行に必要なdesired schema stateが現在のNotion上で
成立している」ことだけである(別actorが同じschemaを用意した可能性を
理論上排除できないため)。read-after-write consistencyやPATCHの
multi-property atomicityはNotion公式ドキュメントで保証が確認できなかった
(Phase 3A監査時点)ため、verificationは1回のGETのみに限定し、polling・
リトライは一切行わない。

Phase 3B-R0監査で、Phase 3B初期実装がこの2つの異なるprovenance(PATCH自体の
transport/write完了事実 と post-write read-backによるdesired-state確認)を
`SchemaWriteCompletion.SUCCEEDED`という単一の値へ潰していたcontract gapが
発覚した(Phase 2O以来、SUCCEEDEDは一貫して「PATCHが例外なくreturnした」
ことだけを意味していた)。Phase 3B-R1でこれを分離する: `SchemaWriteCompletion`
はPATCH自体のtransport/write完了事実のみを表し続け(UNKNOWN+positive
verificationでもcompletionをSUCCEEDEDへ書き換えない)、read-back検証結果は
独立した`SchemaVerificationState`として`SchemaMigrationResult.verification`
に保持する。dedupe phaseへ進んでよいかは`schema_prerequisite_satisfied()`で
判定する(completion単独でもverification単独でもなく、両方を見る)。
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


class SchemaVerificationState:
    """UNKNOWN completion後のpositive-only read-back verificationの結果。

    SchemaWriteCompletion(PATCH自体のtransport/write完了事実)とは独立した
    別のprovenanceを表す――「PATCH自体が成功したか」ではなく「dedupe実行に
    必要なdesired schema stateが確認できたか」を表す(Phase 3B-R0監査で
    この2つを混同していたcontract gapが発覚したため、Phase 3B-R1で分離)。
    """

    #: completionがSUCCEEDEDまたはKNOWN_FAILEDのため、そもそも
    #: verificationを試みていない。
    NOT_ATTEMPTED = "not_attempted"
    #: completion==UNKNOWNの場合に限りverificationを1回試み、要求された
    #: 全propertyが期待するtypeで確認できた。
    DESIRED_STATE_VERIFIED = "desired_state_verified"
    #: completion==UNKNOWNの場合にverificationを試みたが、property欠落・
    #: 型不一致・read-back失敗・応答形状異常のいずれかにより確認できなかった
    #: (original UNKNOWNをそのまま維持すべき状態)。
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class SchemaMigrationResult:
    """schema write(1回のPATCH)の実行結果。

    completion: PATCH自体の完了について分かっている事実(SchemaWriteCompletion)。
    property_names: この1回のPATCHに含まれていたプロパティ名の一覧であり、
      property単位のsuccess/failure情報ではない(1 PATCH全体の結果を表すだけ)。
    verification: completion==UNKNOWNの場合のみ意味を持つ、独立した
      post-write read-back verificationの結果(SchemaVerificationState)。
      completionをSUCCEEDEDへ書き換える根拠には使わない――「PATCH自体が
      成功した」ことの証明ではなく「dedupe実行に必要なdesired schema state
      が現在成立している」ことの確認に過ぎないため、両者の値を混同しない。
    """

    completion: str
    property_names: tuple[str, ...]
    verification: str = SchemaVerificationState.NOT_ATTEMPTED


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
    - read-back自体がNotionAPIErrorで失敗した(timeout/HTTPエラー問わず)
    - responseの形状が想定外(dict以外、properties欠落/非dict、property
      entryが非dict等――Notion API自体がこの形状を絶対に返さないという
      保証はsource上どこにも存在しないため、防御的に扱う)

    fail-closed境界: 明示的なshape validation(isinstance)を先に行い、
    想定される異常形状はそれぞれ個別にFalseを返す。それでも予見できない
    parsing/属性アクセス例外が発生した場合に備え、関数全体を
    `except Exception` で最終防御する(KeyboardInterrupt/SystemExit等の
    BaseExceptionは対象外)。verificationはbest-effort補助チェックに過ぎず、
    ここで何が起きてもoriginal UNKNOWN(元のPATCH timeout等)を決して
    maskしてはならないため、既知の例外型を列挙する通常の狭いcatchより
    安全側を優先する(Phase 3B-R0監査で、未捕捉のAttributeErrorが
    original UNKNOWNをmaskしraw tracebackでcommandを終了させることを
    実際に確認したため)。このtry/exceptはread-back/parsing処理だけを囲み、
    schema PATCHの書き込み自体(既にこの関数の外で完了・失敗している)は
    囲まない。

    read-backは1回のみ試行する(pollingやリトライは行わない)。
    """
    try:
        schema = repo.get_schema()
        if not isinstance(schema, dict):
            return False
        schema_properties = schema.get("properties")
        if not isinstance(schema_properties, dict):
            return False

        for name in property_names:
            expected_definition = SCHEMA_ADDITIONS.get(name)
            if expected_definition is None:
                return False
            expected_type = next(iter(expected_definition))
            actual_property = schema_properties.get(name)
            if not isinstance(actual_property, dict):
                return False
            if actual_property.get("type") != expected_type:
                return False

        return True
    except Exception:  # noqa: BLE001 — verificationはbest-effortでありoriginal
        # UNKNOWNを決してmaskしない最終防御(理由は本docstring参照)。
        return False


def schema_prerequisite_satisfied(result: SchemaMigrationResult) -> bool:
    """dedupe phaseへ進んでよいかを判定する。

    schema write自体のoutcome(completion)そのものではなく、「dedupe実行に
    必要なdesired schema stateが現在成立しているか」を答える。直接成功
    (completion==SUCCEEDED)はもちろん満たすが、completionがUNKNOWNの
    ままでもverification==DESIRED_STATE_VERIFIEDであれば満たす
    (Phase 3B-R1: write outcomeとcan-proceed判定を分離する)。
    """
    if result.completion == SchemaWriteCompletion.SUCCEEDED:
        return True
    return (
        result.completion == SchemaWriteCompletion.UNKNOWN
        and result.verification == SchemaVerificationState.DESIRED_STATE_VERIFIED
    )


def execute_schema_migration(
    repo: DedupeRepository, property_names: list[str]
) -> SchemaMigrationResult:
    """schema migration(1回のPATCH)を実行し、結果を構造化して返す。

    成功時はSchemaMigrationResult(completion=SUCCEEDED, verification=
    NOT_ATTEMPTED)を返す。失敗時はDedupeRepository.apply_schema_migration()
    が送出するNotionAPIErrorの__cause__の型だけ(str(exc)のメッセージは
    一切見ない)からKNOWN_FAILED/UNKNOWNを判定する。

    KNOWN_FAILEDの場合はverificationを一切試みず、常に
    SchemaMigrationExecutionErrorとして再送出する(read-backによって
    KNOWN_FAILEDを覆さない方針、Phase 3B以来不変)。

    completionがUNKNOWNの場合に限り、verify_schema_properties_present()で
    positive-only read-back verificationを1回だけ試みる。

    - 要求されたproperty全てが期待するtypeで確認できた場合
      (verification=DESIRED_STATE_VERIFIED): **completionはUNKNOWNのまま**
      SchemaMigrationResultを正常returnする(Phase 3B-R0で確認された
      result fidelity gapの修正――「今回のPATCHが成功した」という別の
      provenanceの事実へすり替えず、write outcomeとしては引き続き未確定の
      ままにする)。呼び出し元は、dedupeに必要なdesired schema stateが
      満たされていることを理由に(schema_prerequisite_satisfied()経由で)
      後続処理へ進んでよい。
    - 確認できなかった場合(verification=INCONCLUSIVE): KNOWN_FAILEDと
      同様にSchemaMigrationExecutionErrorとして再送出する。

    schema failureは引き続き例外として呼び出し元へ伝播する(control flowを
    変更しない――呼び出し元の単一tryブロックが、schema失敗時にdedupe phase
    へ進まないという既存のfail-closed挙動を構造的に維持する。
    verification=DESIRED_STATE_VERIFIEDの場合のみ例外的に正常returnし、
    dedupe phaseへの進行を許可する)。
    """
    try:
        repo.apply_schema_migration(property_names)
    except NotionAPIError as exc:
        completion = _completion_from_notion_api_error(exc)
        if completion == SchemaWriteCompletion.UNKNOWN:
            if verify_schema_properties_present(repo, tuple(property_names)):
                return SchemaMigrationResult(
                    completion=SchemaWriteCompletion.UNKNOWN,
                    property_names=tuple(property_names),
                    verification=SchemaVerificationState.DESIRED_STATE_VERIFIED,
                )
            verification = SchemaVerificationState.INCONCLUSIVE
        else:
            verification = SchemaVerificationState.NOT_ATTEMPTED
        raise SchemaMigrationExecutionError(
            SchemaMigrationResult(
                completion=completion,
                property_names=tuple(property_names),
                verification=verification,
            ),
            str(exc),
        ) from exc
    return SchemaMigrationResult(
        completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(property_names)
    )

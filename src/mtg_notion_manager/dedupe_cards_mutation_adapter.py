"""dedupe-cards自身のExecution(schema mutation + dedupe group mutation)を、
既存のdedupe-family共有adapter(dedupe_apply_mutation_adapter.py)経由で
Error Contract v2のMutationSummaryへ変換するための専用adapter。

apply-dedupe-plan/apply-price-link-dedupeと異なり、dedupe-cardsは:

1. build_dedupe_plan()/execute_dedupe_plan()をfreshness re-audit無しで
   直接呼ぶ(dedupe_cards.GroupApplyResultにはapply-dedupe-plan等の
   GroupApplyOutcomeが持つstatus/merged_page_idsフィールドが無いため、
   dedupe_apply_mutation_adapter.DedupeGroupOutcome Protocolを満たす
   薄いwrapperが必要)。
2. schema mutation(--apply-schema、services/dedupe_schema.py、Phase 2O)
   というdedupe writeとは別起源のwrite attemptを同一invocation内に持つ。

という2点で構造が異なる。このmoduleはその差分だけを吸収し、
dedupe_apply_mutation_adapter.py自体のcount/state/recovery導出ロジックは
一切複製しない(extra_*パラメータへ委譲するのみ)。

services/dedupe_schema.py自体はError Contract(mutation_contract.py/
error_contract.py)へ一切依存しない設計を維持している(Phase 2Oの方針)。
このmoduleがその境界を越える変換責務を担う。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mtg_notion_manager.dedupe_apply_mutation_adapter import build_dedupe_apply_mutation_summary
from mtg_notion_manager.mutation_contract import MutationOperation, MutationSummary
from mtg_notion_manager.services.dedupe_cards import DedupeApplyResult
from mtg_notion_manager.services.dedupe_schema import (
    SchemaMigrationExecutionError,
    SchemaMigrationResult,
    SchemaWriteCompletion,
)

_STATUS_APPLIED = "applied"
_STATUS_FAILED = "failed"
_SCHEMA_UPDATE_ACTION = "schema_update"


@dataclass(frozen=True)
class _DedupeCardsGroupOutcome:
    """dedupe_cards.GroupApplyResultを、dedupe_apply_mutation_adapter.DedupeGroupOutcome
    Protocolが要求する形へ変換した読み取り専用view。

    dedupe-cardsはapply-dedupe-plan/apply-price-link-dedupeと異なり
    freshness re-auditを行わないため、statusはAPPLIED/FAILEDの2値のみ
    (PLANNED/SKIPPED_*相当は存在しない)。
    """

    card_name: str
    status: str
    merged_page_ids: list[str]
    failed_operation: str | None
    failed_completion: str | None


def _wrap_group_apply_results(dedupe_result: DedupeApplyResult) -> list[_DedupeCardsGroupOutcome]:
    return [
        _DedupeCardsGroupOutcome(
            card_name=r.card_name,
            status=_STATUS_FAILED if r.error is not None else _STATUS_APPLIED,
            merged_page_ids=r.duplicate_page_ids_marked,
            failed_operation=r.failed_operation,
            failed_completion=r.failed_completion,
        )
        for r in dedupe_result.results
    ]


@dataclass(frozen=True)
class SchemaMutationContribution:
    """schema mutation(1回のPATCH、試行していなければ全て0)がaggregate
    MutationSummaryへ加算するべきcount/operations。schema PATCHは常に0か1回
    のみ試行される(single-PATCH契約、Phase 2Oから変更なし)ため、
    succeeded/failed/unknownのいずれか1つだけが1で、他は0になる
    (試行していない場合は全て0)。
    """

    succeeded: int = 0
    failed: int = 0
    unknown: int = 0
    operations: tuple[MutationOperation, ...] = field(default_factory=tuple)


#: schema writeを一切試みていない場合(--apply-schema省略・schema既存・dry-run)。
NO_SCHEMA_CONTRIBUTION = SchemaMutationContribution()


def schema_mutation_contribution_for_success(
    result: SchemaMigrationResult,
) -> SchemaMutationContribution:
    """成功operationはoperations[]へ含めない(既存dedupe adapterと同じ方針、
    §26/§27: property単位のwrite accountingを新設しない)。
    """
    del result
    return SchemaMutationContribution(succeeded=1)


def schema_mutation_contribution_for_failure(
    exc: SchemaMigrationExecutionError,
) -> SchemaMutationContribution:
    """exc.result.completion(KNOWN_FAILED/UNKNOWN)だけから導出する
    (str(exc)のmessageは一切参照しない、Phase 2Oのmessage-independent
    classification方針を維持)。
    """
    key = "、".join(exc.result.property_names)
    if exc.result.completion == SchemaWriteCompletion.KNOWN_FAILED:
        return SchemaMutationContribution(
            failed=1,
            operations=(MutationOperation(key=key, action=_SCHEMA_UPDATE_ACTION, state="failed"),),
        )
    if exc.result.completion == SchemaWriteCompletion.UNKNOWN:
        return SchemaMutationContribution(
            unknown=1,
            operations=(MutationOperation(key=key, action=_SCHEMA_UPDATE_ACTION, state="unknown"),),
        )
    raise ValueError(
        "Unexpected SchemaWriteCompletion on a failure carrier: "
        f"{exc.result.completion!r} (card DB schema properties: {exc.result.property_names!r})."
    )


def build_dedupe_cards_mutation_summary(
    dedupe_result: DedupeApplyResult | None,
    schema_contribution: SchemaMutationContribution,
) -> MutationSummary:
    """dedupe-cards 1 invocation分のaggregate MutationSummaryを構築する。

    dedupe_result が None の場合(dedupe phaseへ到達しなかった、すなわち
    schema mutationが失敗/unknownでdedupe phaseへ進まなかった場合、Phase 2Oの
    fail-closed契約)は、dedupe側のwrite試行を0件として扱う
    (build_dedupe_apply_mutation_summary([])と等価)。
    """
    outcomes = _wrap_group_apply_results(dedupe_result) if dedupe_result is not None else []
    return build_dedupe_apply_mutation_summary(
        outcomes,
        extra_succeeded=schema_contribution.succeeded,
        extra_failed=schema_contribution.failed,
        extra_unknown=schema_contribution.unknown,
        extra_operations=schema_contribution.operations,
    )

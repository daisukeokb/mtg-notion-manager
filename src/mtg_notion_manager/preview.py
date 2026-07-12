"""import-cards / dedupe-cards のdry-run/適用結果のサマリー・差分表示。"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from mtg_notion_manager.services.dedupe_cards import DedupeApplyResult, DedupePlan
from mtg_notion_manager.services.import_cards import ImportCardsPlan, ImportCardsResult


def print_plan_summary(console: Console, plan: ImportCardsPlan) -> None:
    counts = plan.summary
    existing_count = counts.get("relation_update", 0) + counts.get("unchanged", 0)

    console.print(f"デッキ: {plan.parsed.deck_name}")
    console.print(f"抽出枚数: {plan.parsed.total_quantity}")
    console.print(f"ユニークカード数: {len(plan.parsed.cards)}")
    console.print()
    console.print(f"既存カード: {existing_count}")
    console.print(f"新規作成予定: {counts.get('create', 0)}")
    console.print(f"リレーション追加予定: {counts.get('relation_update', 0)}")
    console.print(f"変更なし: {counts.get('unchanged', 0)}")
    console.print(f"曖昧一致: {counts.get('ambiguous', 0)}")
    console.print(f"エラー: {counts.get('error', 0)}")


def print_plan_detail(console: Console, plan: ImportCardsPlan) -> None:
    table = Table(title="カード別詳細")
    table.add_column("日本語名")
    table.add_column("英語名")
    table.add_column("枚数", justify="right")
    table.add_column("判定")
    table.add_column("対象ページID")
    table.add_column("差分")

    for decision in plan.decisions:
        card = decision.card
        page_id = decision.existing.page_id if decision.existing else ""
        table.add_row(
            card.name_ja or "",
            card.name_en or "",
            str(card.quantity),
            decision.action,
            page_id,
            decision.detail,
        )
    console.print(table)


def print_apply_result(console: Console, result: ImportCardsResult) -> None:
    table = Table(title="適用結果")
    table.add_column("カード")
    table.add_column("結果")
    table.add_column("ページID")
    table.add_column("エラー")

    for r in result.results:
        table.add_row(r.card.display_name, r.action, r.page_id or "", r.error or "")
    console.print(table)

    succeeded = len(result.succeeded)
    failed = len(result.failed)
    console.print(f"成功: {succeeded}件 / 失敗: {failed}件")
    if failed:
        console.print(
            "[red]失敗したカードがあります。同じコマンドを再実行してください"
            "(成功済みのカードは重複作成・重複リレーションされません)。[/red]"
        )


def print_dedupe_schema_plan(console: Console, missing_properties: list[str]) -> None:
    if not missing_properties:
        console.print("スキーマ: 必要なプロパティは既に存在します。")
        return
    console.print("[yellow]スキーマ変更が必要です(未追加のプロパティ):[/yellow]")
    for name in missing_properties:
        console.print(f"  - {name}")
    console.print("--apply-schema を指定すると追加します(既存プロパティには影響しません)。")


def print_dedupe_plan(console: Console, plan: DedupePlan) -> None:
    console.print(f"重複グループ数: {len(plan.merge_plans)}")
    console.print(f"検出エラー: {len(plan.group_errors)}")
    console.print()

    for merge_plan in plan.merge_plans:
        console.print(f"カード名: {merge_plan.group.card_name}")
        console.print(f"重複レコード: {len(merge_plan.group.pages)}")
        console.print(f"代表候補: {merge_plan.representative_page_id}")
        console.print("代表選択理由:")
        for reason in merge_plan.representative.reasons:
            console.print(f"  - {reason}")
        console.print()
        console.print("統合後:")
        console.print(f"  - 所持: {merge_plan.owned}")
        console.print(f"  - 所持枚数: {merge_plan.quantity}")
        console.print(f"  - 採用デッキ: {len(merge_plan.merged_deck_relation_ids)}件")
        console.print(f"  - 英語名: {merge_plan.english_name or '(なし)'}")
        console.print()
        console.print(f"統合済み設定予定: {len(merge_plan.duplicate_pages)}件")
        console.print("削除予定: 0件")
        console.print("元ページ:")
        for page in merge_plan.group.pages:
            marker = "[代表]" if page["id"] == merge_plan.representative_page_id else "[統合対象]"
            console.print(f"  {marker} {page.get('url', page['id'])}")
        console.print()

    for group_error in plan.group_errors:
        console.print(f"[red]カード名: {group_error.card_name} — {group_error.error_type}[/red]")
        console.print(f"  {group_error.message}")
        for page in group_error.pages:
            console.print(f"  - {page.get('url', page['id'])}")
        console.print()


def print_dedupe_apply_result(console: Console, result: DedupeApplyResult) -> None:
    table = Table(title="統合結果")
    table.add_column("カード名")
    table.add_column("代表ページID")
    table.add_column("統合済み設定件数", justify="right")
    table.add_column("エラー")

    for r in result.results:
        table.add_row(
            r.card_name,
            r.representative_page_id,
            str(len(r.duplicate_page_ids_marked)),
            r.error or "",
        )
    console.print(table)

    succeeded = len(result.succeeded)
    failed = len(result.failed)
    console.print(f"成功: {succeeded}件 / 失敗: {failed}件")
    if failed:
        console.print(
            "[red]失敗したグループがあります。同じコマンドを再実行してください"
            "(既に統合済みのページは再処理されません)。[/red]"
        )

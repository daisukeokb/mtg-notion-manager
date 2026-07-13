"""import-cards / dedupe-cards のdry-run/適用結果のサマリー・差分表示。"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from mtg_notion_manager.services.card_resolution import ResolutionSummary, summarize_decisions
from mtg_notion_manager.services.dedupe_cards import DedupeApplyResult, DedupePlan
from mtg_notion_manager.services.import_article import (
    STATUS_LABELS,
    STATUS_READY,
    ArticleImportPlan,
)
from mtg_notion_manager.services.import_cards import ImportCardsPlan, ImportCardsResult
from mtg_notion_manager.services.verify_import import ArticleVerifyReport


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
    console.print()
    print_resolution_summary(console, summarize_decisions(plan.decisions))


def print_resolution_summary(console: Console, summary: ResolutionSummary) -> None:
    """新規カードのprovenance別内訳(全実行対象での書き込み可否判定を含む)。"""
    console.print(
        f"記事由来日本語名で作成可能: {summary.creatable_from_article_japanese_name_count}"
    )
    console.print(f"人間確認済みで作成可能: {summary.creatable_from_human_confirmation_count}")
    console.print(f"確認待ち: {summary.pending_confirmation_count}")
    console.print(f"identity conflict: {summary.identity_conflict_count}")
    console.print(f"設定エラー: {summary.config_error_count}")


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


def print_article_plan_summary(console: Console, plan: ArticleImportPlan) -> None:
    counts = plan.counts
    console.print(f"検出デッキ数: {len(plan.all_deck_names)}")
    if plan.excluded_deck_names:
        console.print(f"除外デッキ: {', '.join(plan.excluded_deck_names)}")
    console.print(f"処理可能: {counts[STATUS_READY]}")
    console.print(f"要確認: {counts['needs_review']}")
    console.print(f"エラー: {counts['error']}")
    console.print()

    all_decisions = [
        decision
        for entry in plan.entries
        if entry.cards_plan is not None
        for decision in entry.cards_plan.decisions
    ]
    resolution_summary = summarize_decisions(all_decisions)
    console.print(f"既存カード: {resolution_summary.existing_count}")
    print_resolution_summary(console, resolution_summary)
    console.print(f"実適用可能: {'はい' if plan.is_fully_applicable else 'いいえ'}")
    console.print()

    table = Table(title="デッキ別サマリー")
    table.add_column("デッキ名")
    table.add_column("状態")
    table.add_column("抽出枚数", justify="right")
    table.add_column("ユニーク", justify="right")
    table.add_column("既存", justify="right")
    table.add_column("新規", justify="right")
    table.add_column("リレーション追加", justify="right")
    table.add_column("変更なし", justify="right")
    table.add_column("曖昧一致", justify="right")
    table.add_column("エラー", justify="right")
    table.add_column("備考")

    for entry in plan.entries:
        if entry.cards_plan is not None:
            counts_by_action = entry.cards_plan.summary
            parsed = entry.cards_plan.parsed
            existing = counts_by_action.get("relation_update", 0) + counts_by_action.get(
                "unchanged", 0
            )
            table.add_row(
                entry.deck_name,
                STATUS_LABELS[entry.status],
                str(parsed.total_quantity),
                str(len(parsed.cards)),
                str(existing),
                str(counts_by_action.get("create", 0)),
                str(counts_by_action.get("relation_update", 0)),
                str(counts_by_action.get("unchanged", 0)),
                str(counts_by_action.get("ambiguous", 0)),
                str(counts_by_action.get("error", 0)),
                entry.reason,
            )
        else:
            table.add_row(
                entry.deck_name,
                STATUS_LABELS[entry.status],
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                entry.reason,
            )
    console.print(table)


def print_article_deck_detail(console: Console, plan: ArticleImportPlan) -> None:
    for entry in plan.entries:
        console.print(f"[bold]デッキ: {entry.deck_name}[/bold] ({STATUS_LABELS[entry.status]})")
        console.print(f"  解決方法: {entry.resolution_method or '(未解決)'}")
        if entry.deck_page_id:
            console.print(f"  Notionページ: {entry.deck_page_url or entry.deck_page_id}")
        if entry.reason:
            console.print(f"  理由: {entry.reason}")
        if entry.cards_plan is not None:
            print_plan_detail(console, entry.cards_plan)
        console.print()


def print_article_apply_result(console: Console, plan: ArticleImportPlan) -> None:
    table = Table(title="デッキ別適用結果")
    table.add_column("デッキ名")
    table.add_column("状態")
    table.add_column("作成")
    table.add_column("リレーション追加")
    table.add_column("変更なし")
    table.add_column("失敗")
    table.add_column("備考")

    for entry in plan.entries:
        if entry.apply_result is not None:
            results = entry.apply_result.results
            table.add_row(
                entry.deck_name,
                STATUS_LABELS[entry.status],
                str(sum(1 for r in results if r.action == "created")),
                str(sum(1 for r in results if r.action == "relation_updated")),
                str(sum(1 for r in results if r.action == "unchanged")),
                str(sum(1 for r in results if r.action == "failed")),
                entry.reason,
            )
        else:
            table.add_row(
                entry.deck_name, STATUS_LABELS[entry.status], "-", "-", "-", "-", entry.reason
            )
    console.print(table)


def print_verify_import_summary(console: Console, report: ArticleVerifyReport) -> None:
    summary = report.summary
    console.print(f"検出デッキ数: {len(report.all_deck_names)}")
    console.print(f"対象デッキ数: {summary['selected_deck_count']}")
    console.print(f"成功数: {summary['verified_deck_count']}")
    console.print(f"失敗数: {summary['mismatch_deck_count']}")
    console.print(f"新規カード数: {summary['total_new_card_count']}")
    console.print(f"曖昧一致数: {summary['total_ambiguous_match_count']}")
    console.print(f"不足relation数: {summary['total_missing_relation_count']}")
    console.print(f"余分relation数: {summary['total_unexpected_relation_count']}")
    console.print(f"エラー数: {summary['total_error_count']}")
    console.print()

    table = Table(title="デッキ別検証結果")
    table.add_column("デッキ名")
    table.add_column("結果")
    table.add_column("抽出枚数", justify="right")
    table.add_column("ユニーク", justify="right")
    table.add_column("新規", justify="right")
    table.add_column("曖昧一致", justify="right")
    table.add_column("不足relation", justify="right")
    table.add_column("余分relation", justify="right")
    table.add_column("備考")

    for entry in report.entries:
        result_label = "OK" if entry.is_verified else "NG"
        table.add_row(
            entry.deck_name,
            result_label,
            str(entry.extracted_card_count) if entry.extracted_card_count is not None else "-",
            str(entry.unique_card_count) if entry.unique_card_count is not None else "-",
            str(entry.new_card_count) if entry.new_card_count is not None else "-",
            str(entry.ambiguous_match_count) if entry.ambiguous_match_count is not None else "-",
            str(len(entry.missing_relation_page_ids)),
            str(len(entry.unexpected_relation_page_ids)),
            "; ".join(entry.verification_errors),
        )
    console.print(table)


def print_verify_import_detail(console: Console, report: ArticleVerifyReport) -> None:
    for entry in report.entries:
        result_label = "OK" if entry.is_verified else "NG"
        console.print(f"[bold]デッキ: {entry.deck_name}[/bold] ({result_label})")
        console.print(f"  解決方法: {entry.resolution_method or '(未解決)'}")
        if entry.deck_page_id:
            console.print(f"  Notionページ: {entry.deck_page_url or entry.deck_page_id}")
        for error in entry.verification_errors:
            console.print(f"  - {error}")
        if entry.missing_relation_cards:
            console.print("  不足relation:")
            for card in entry.missing_relation_cards:
                console.print(
                    f"    - {card['page_id']} ({card['name_ja'] or card['name_en'] or '?'})"
                )
        if entry.unexpected_relation_cards:
            console.print("  余分relation:")
            for card in entry.unexpected_relation_cards:
                console.print(
                    f"    - {card['page_id']} ({card['name_ja'] or card['name_en'] or '?'})"
                )
        console.print()

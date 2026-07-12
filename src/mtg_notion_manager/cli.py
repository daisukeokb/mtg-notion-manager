import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from mtg_notion_manager.config import Config, ConfigError
from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.preview import (
    print_apply_result,
    print_dedupe_apply_result,
    print_dedupe_plan,
    print_dedupe_schema_plan,
    print_plan_detail,
    print_plan_summary,
)
from mtg_notion_manager.services.apply_dedupe_plan import (
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_SKIPPED_NOT_DUPLICATE,
    STATUS_SKIPPED_STALE,
    apply_dedupe_batch,
    load_audit_report,
    select_target_groups,
    write_apply_log,
)
from mtg_notion_manager.services.audit_duplicates import (
    CATEGORY_AUTO,
    CATEGORY_EXCLUDED,
    CATEGORY_LABELS,
    CATEGORY_MANUAL_REPRESENTATIVE,
    CATEGORY_NEEDS_REVIEW,
    audit_duplicate_groups,
    load_exclusions,
    write_audit_reports,
)
from mtg_notion_manager.services.dedupe_cards import build_dedupe_plan, execute_dedupe_plan
from mtg_notion_manager.services.doctor import run_doctor
from mtg_notion_manager.services.import_cards import build_import_cards_plan, execute_import_cards
from mtg_notion_manager.services.import_deck import build_import_plan, execute_import

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.callback()
def main() -> None:
    """MTG統率者デッキをNotionで管理するCLIツール。"""


@app.command(name="doctor")
def doctor_command() -> None:
    """Notion認証・DB接続・スキーマの健全性を診断する。"""
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        with NotionClient(config.notion_api_key) as client:
            results = run_doctor(config, client)
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="doctor診断結果")
    table.add_column("チェック項目")
    table.add_column("結果")
    table.add_column("詳細")

    all_ok = True
    for result in results:
        status = "[green]OK[/green]" if result.ok else "[red]NG[/red]"
        if not result.ok:
            all_ok = False
        table.add_row(result.name, status, result.message)

    console.print(table)

    if not all_ok:
        console.print("[red]一部のチェックに失敗しました。[/red]")
        raise typer.Exit(code=1)
    console.print("[green]すべてのチェックに合格しました。[/green]")


@app.command(name="import")
def import_command(
    url: str = typer.Argument(
        ..., help="統率者デッキ紹介ページのURL(magic.wizards.com または mtg-jp.com)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Notionへは書き込まず、プレビューのみ表示する"
    ),
    deck_name: str = typer.Option(
        None,
        "--deck-name",
        help="1ページに複数デッキが含まれる場合に対象デッキ名を指定する",
    ),
) -> None:
    """デッキ情報を取得してNotionのMTG統率者DBに登録する。"""
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        with NotionClient(config.notion_api_key) as client:
            writer = NotionWriter(client, config.commander_data_source_id)
            plan = build_import_plan(url, writer, deck_name)

            console.print("[bold]プレビュー[/bold]")
            console.print_json(json.dumps(plan.record.to_preview_dict(), ensure_ascii=False))

            if plan.existing is not None:
                console.print(
                    f"[yellow]同名デッキが既に存在します:[/yellow] {plan.existing.page_url}"
                )
                if plan.diff:
                    table = Table(title="差分")
                    table.add_column("プロパティ")
                    table.add_column("既存値")
                    table.add_column("新しい値")
                    for entry in plan.diff:
                        table.add_row(
                            entry.property_name,
                            str(entry.existing_value),
                            str(entry.new_value),
                        )
                    console.print(table)
                else:
                    console.print("差分はありません(完全に一致しています)。")
                console.print("[yellow]重複のため登録をスキップしました。[/yellow]")
                raise typer.Exit(code=0)

            if dry_run:
                console.print("[cyan]--dry-run のためNotionへの書き込みは行いません。[/cyan]")
                raise typer.Exit(code=0)

            if not typer.confirm("この内容でNotionに登録しますか?"):
                console.print("キャンセルしました。")
                raise typer.Exit(code=0)

            execute_import(plan, writer)
            console.print("[green]Notionに登録しました。[/green]")

    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command(name="import-cards")
def import_cards_command(
    url: str = typer.Argument(
        ..., help="統率者デッキ紹介ページのURL(magic.wizards.com または mtg-jp.com)"
    ),
    deck_name: str = typer.Option(
        None,
        "--deck-name",
        help="対象デッキ名。1ページに複数デッキが含まれる場合は必須。"
        " --deck-page-id 省略時はこの名前でMTG統率者DBを検索する",
    ),
    deck_page_id: str = typer.Option(
        None,
        "--deck-page-id",
        help="対象デッキのNotionページID(省略時は --deck-name でMTG統率者DBを検索する)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="取得・解析・照合・差分表示のみ行い、Notionへは書き込まない"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="実際にNotionへ書き込む(指定しない限り書き込まない)"
    ),
    update: bool = typer.Option(
        False,
        "--update",
        help="既存カードの手動管理値を上書きする場合に指定する"
        "(現バージョンは所持・採用デッキのみ自動更新するため未使用、将来のため予約)",
    ),
    allow_count_mismatch: bool = typer.Option(
        False, "--allow-count-mismatch", help="デッキ合計枚数が100枚でなくても続行する"
    ),
    show_detail: bool = typer.Option(
        True, "--detail/--no-detail", help="カード別詳細テーブルを表示する"
    ),
) -> None:
    """デッキのカード一式をMTGカードDBへ登録し、対象デッキとリレーションする。"""
    del update  # 将来の拡張用に予約(現バージョンでは挙動に影響しない)

    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=1)

    if deck_page_id is None and deck_name is None:
        console.print(
            "[red]エラー:[/red] --deck-page-id か --deck-name のいずれかを指定してください。"
        )
        raise typer.Exit(code=1)

    try:
        with NotionClient(config.notion_api_key) as client:
            resolved_deck_page_id = deck_page_id
            if resolved_deck_page_id is None:
                writer = NotionWriter(client, config.commander_data_source_id)
                existing_deck = writer.find_existing_deck(deck_name)
                if existing_deck is None:
                    console.print(
                        f"[red]エラー:[/red] MTG統率者DBに '{deck_name}' が見つかりません。"
                        " 先に `import` コマンドでデッキを登録してください。"
                    )
                    raise typer.Exit(code=1)
                resolved_deck_page_id = existing_deck.page_id

            card_repo = CardRepository(client, config.card_data_source_id)
            plan = build_import_cards_plan(
                url,
                resolved_deck_page_id,
                card_repo,
                deck_name=deck_name,
                allow_count_mismatch=allow_count_mismatch,
            )

            print_plan_summary(console, plan)
            console.print()
            if show_detail:
                print_plan_detail(console, plan)

            if plan.has_blocking_issues:
                console.print(
                    "[red]曖昧一致または未解決のカードがあるため、"
                    "--apply を指定しても書き込みは行われません。[/red]"
                )

            if dry_run or not apply:
                console.print(
                    "[cyan]--apply が指定されていないため、Notionへの書き込みは行いません。[/cyan]"
                )
                raise typer.Exit(code=0)

            note = f"{plan.parsed.deck_name}プレコン由来"
            result = execute_import_cards(plan, card_repo, note=note)
            console.print()
            print_apply_result(console, result)

            if result.failed:
                raise typer.Exit(code=1)

    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command(name="dedupe-cards")
def dedupe_cards_command(
    card_name: str = typer.Option(
        None, "--card-name", help="対象カード名を1件に絞り込む(省略時は全重複グループが対象)"
    ),
    representative_page_id: str = typer.Option(
        None,
        "--representative-page-id",
        help="代表レコードを手動指定する(--card-name と併用する場合のみ有効)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="検出・統合計画の表示のみ行い、Notionへは書き込まない"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="実際にNotionへ統合を書き込む(指定しない限り書き込まない)"
    ),
    apply_schema: bool = typer.Option(
        False,
        "--apply-schema",
        help="不足しているスキーマ(所持枚数・統合済み)をNotionデータベースへ追加する",
    ),
    apply_all: bool = typer.Option(
        False,
        "--apply-all",
        help="--card-name 省略時に全重複グループへ適用する(--yes との併用が必須)",
    ),
    yes: bool = typer.Option(False, "--yes", help="--apply-all の確認"),
) -> None:
    """MTGカードDBの重複カード(同名複数ページ)を検出し、代表レコードへ安全に統合する。"""
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=1)

    if representative_page_id is not None and card_name is None:
        console.print(
            "[red]エラー:[/red] --representative-page-id は --card-name と併用してください。"
        )
        raise typer.Exit(code=1)

    if apply and card_name is None and not (apply_all and yes):
        console.print(
            "[red]エラー:[/red] --card-name を指定しない全件適用には"
            " --apply-all と --yes の両方が必要です。"
        )
        raise typer.Exit(code=1)

    try:
        with NotionClient(config.notion_api_key) as client:
            repo = DedupeRepository(client, config.card_data_source_id)

            missing_schema = repo.missing_schema_properties()
            print_dedupe_schema_plan(console, missing_schema)
            console.print()

            if apply_schema and missing_schema:
                repo.apply_schema_migration(missing_schema)
                console.print(f"[green]スキーマに追加しました: {', '.join(missing_schema)}[/green]")
                missing_schema = []
                console.print()

            plan = build_dedupe_plan(
                repo, card_name=card_name, representative_page_id=representative_page_id
            )
            print_dedupe_plan(console, plan)

            if not plan.merge_plans:
                console.print("統合対象の重複はありませんでした。")
                raise typer.Exit(code=0)

            if dry_run or not apply:
                console.print(
                    "[cyan]--apply が指定されていないため、Notionへの書き込みは行いません。[/cyan]"
                )
                raise typer.Exit(code=0)

            if missing_schema:
                console.print(
                    "[red]エラー:[/red] 必要なスキーマ("
                    f"{', '.join(missing_schema)}) が存在しないため書き込みできません。"
                    " --apply-schema を指定してください。"
                )
                raise typer.Exit(code=1)

            result = execute_dedupe_plan(plan, repo)
            console.print()
            print_dedupe_apply_result(console, result)

            if result.failed:
                raise typer.Exit(code=1)

    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command(name="audit-duplicates")
def audit_duplicates_command(
    card_name: str = typer.Option(
        None, "--card-name", help="対象カード名を1件に絞り込む(省略時は全重複グループ)"
    ),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="レポート(JSON/CSV/Markdown)の出力先ディレクトリ"
    ),
) -> None:
    """残りの重複カードグループを監査し、統合可否を分類したレポートを出力する。

    Notionへは一切書き込まない(読み取り専用)。
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=1)

    exclusions = load_exclusions()

    try:
        with NotionClient(config.notion_api_key) as client:
            repo = DedupeRepository(client, config.card_data_source_id)
            audits = audit_duplicate_groups(repo, card_name=card_name, exclusions=exclusions)
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    paths = write_audit_reports(audits, Path(output_dir))

    counts = {category: 0 for category in CATEGORY_LABELS}
    for audit in audits:
        counts[audit.category] += 1

    console.print(f"全グループ数: {len(audits)}")
    console.print(f"{CATEGORY_LABELS[CATEGORY_AUTO]}: {counts[CATEGORY_AUTO]}")
    console.print(f"{CATEGORY_LABELS[CATEGORY_NEEDS_REVIEW]}: {counts[CATEGORY_NEEDS_REVIEW]}")
    console.print(
        f"{CATEGORY_LABELS[CATEGORY_MANUAL_REPRESENTATIVE]}: "
        f"{counts[CATEGORY_MANUAL_REPRESENTATIVE]}"
    )
    console.print(f"{CATEGORY_LABELS[CATEGORY_EXCLUDED]}: {counts[CATEGORY_EXCLUDED]}")
    console.print()
    console.print("レポートを出力しました:")
    console.print(f"  - {paths.json_path}")
    console.print(f"  - {paths.csv_path}")
    console.print(f"  - {paths.markdown_path}")


@app.command(name="apply-dedupe-plan")
def apply_dedupe_plan_command(
    audit_report: str = typer.Option(..., "--audit-report", help="監査レポートJSONのパス"),
    classification: str = typer.Option(
        "auto", "--classification", help="対象とする分類(現在は auto のみ対応)"
    ),
    limit: int = typer.Option(
        None, "--limit", help="適用する最大グループ数(低リスク順に上から切り出す)"
    ),
    offset: int = typer.Option(0, "--offset", help="低リスク順ソート後の開始位置"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="計画の表示のみ行い、Notionへは書き込まない"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="実際にNotionへ統合を書き込む(指定しない限り書き込まない)"
    ),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="実行ログの出力先ディレクトリ"
    ),
) -> None:
    """監査レポートの「自動統合可能」グループだけを、鮮度チェックのうえ段階適用する。

    適用直前に対象カード名を現在のNotion状態で再監査し、分類やページ構成が
    変化していればそのグループをスキップする(削除APIは一切使用しない)。
    """
    if classification != CATEGORY_AUTO:
        console.print(
            "[red]エラー:[/red] 現在は --classification auto のみサポートしています。"
        )
        raise typer.Exit(code=1)

    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=1)

    try:
        groups = load_audit_report(Path(audit_report), classification=classification)
    except (OSError, ValueError) as exc:
        console.print(f"[red]エラー:[/red] 監査レポートを読み込めませんでした: {exc}")
        raise typer.Exit(code=1) from exc

    targets = select_target_groups(groups, limit=limit, offset=offset)

    console.print(f"対象グループ数: {len(targets)}")
    table = Table(title="適用対象(低リスク順)")
    table.add_column("カード名")
    table.add_column("重複件数", justify="right")
    table.add_column("採用デッキ件数(監査時)", justify="right")
    table.add_column("推奨代表ページID")
    for group in targets:
        table.add_row(
            group.card_name,
            str(group.duplicate_count),
            str(group.merged_deck_relation_count),
            group.recommended_representative_id or "",
        )
    console.print(table)

    if not targets:
        console.print("対象がありません。")
        raise typer.Exit(code=0)

    exclusions = load_exclusions()
    do_apply = apply and not dry_run

    try:
        with NotionClient(config.notion_api_key) as client:
            repo = DedupeRepository(client, config.card_data_source_id)
            outcomes = apply_dedupe_batch(repo, targets, apply=do_apply, exclusions=exclusions)
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print()
    result_table = Table(title="適用結果")
    result_table.add_column("カード名")
    result_table.add_column("結果")
    result_table.add_column("代表ページID")
    result_table.add_column("詳細/エラー")
    for outcome in outcomes:
        result_table.add_row(
            outcome.card_name,
            outcome.status,
            outcome.representative_page_id or "",
            outcome.error or outcome.reason,
        )
    console.print(result_table)

    counts = {
        STATUS_APPLIED: 0,
        STATUS_PLANNED: 0,
        STATUS_SKIPPED_STALE: 0,
        STATUS_SKIPPED_NOT_DUPLICATE: 0,
        STATUS_FAILED: 0,
    }
    for outcome in outcomes:
        counts[outcome.status] += 1

    console.print()
    console.print(f"適用: {counts[STATUS_APPLIED]}件")
    console.print(f"計画のみ(dry-run): {counts[STATUS_PLANNED]}件")
    console.print(
        f"スキップ(鮮度不一致): {counts[STATUS_SKIPPED_STALE]}件"
    )
    console.print(f"スキップ(重複解消済み): {counts[STATUS_SKIPPED_NOT_DUPLICATE]}件")
    console.print(f"失敗: {counts[STATUS_FAILED]}件")

    log_paths = write_apply_log(
        outcomes, audit_report_path=audit_report, output_dir=Path(output_dir), applied=do_apply
    )
    console.print()
    console.print(f"実行ログ: {log_paths.json_path}")

    if not do_apply:
        console.print(
            "[cyan]--apply が指定されていないため、Notionへの書き込みは行いません。[/cyan]"
        )
        raise typer.Exit(code=0)

    if counts[STATUS_FAILED]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

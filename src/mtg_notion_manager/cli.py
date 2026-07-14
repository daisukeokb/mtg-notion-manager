import datetime
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from mtg_notion_manager.card_match_overrides import load_card_match_overrides
from mtg_notion_manager.config import Config, ConfigError
from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.intentional_duplicates import load_intentional_duplicates
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.preview import (
    print_apply_result,
    print_article_apply_result,
    print_article_deck_detail,
    print_article_plan_summary,
    print_dedupe_apply_result,
    print_dedupe_plan,
    print_dedupe_schema_plan,
    print_plan_detail,
    print_plan_summary,
    print_verify_import_detail,
    print_verify_import_summary,
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
from mtg_notion_manager.services.apply_price_link_dedupe import (
    STATUS_APPLIED as PRICE_STATUS_APPLIED,
)
from mtg_notion_manager.services.apply_price_link_dedupe import (
    STATUS_FAILED as PRICE_STATUS_FAILED,
)
from mtg_notion_manager.services.apply_price_link_dedupe import (
    STATUS_PLANNED as PRICE_STATUS_PLANNED,
)
from mtg_notion_manager.services.apply_price_link_dedupe import (
    STATUS_SKIPPED_NOT_DUPLICATE as PRICE_STATUS_SKIPPED_NOT_DUPLICATE,
)
from mtg_notion_manager.services.apply_price_link_dedupe import (
    STATUS_SKIPPED_STALE as PRICE_STATUS_SKIPPED_STALE,
)
from mtg_notion_manager.services.apply_price_link_dedupe import (
    apply_price_link_targets,
    load_price_link_targets,
    select_canary_targets,
    select_remaining_batch,
    write_price_link_apply_log,
)
from mtg_notion_manager.services.audit_duplicates import (
    CATEGORY_AUTO,
    CATEGORY_EXCLUDED,
    CATEGORY_INTENTIONAL_DUPLICATE,
    CATEGORY_LABELS,
    CATEGORY_MANUAL_REPRESENTATIVE,
    CATEGORY_NEEDS_REVIEW,
    audit_duplicate_groups,
    load_exclusions,
    write_audit_reports,
)
from mtg_notion_manager.services.card_resolution import (
    load_confirmed_card_mapping,
    write_pending_manifest,
)
from mtg_notion_manager.services.dedupe_cards import build_dedupe_plan, execute_dedupe_plan
from mtg_notion_manager.services.doctor import run_doctor
from mtg_notion_manager.services.import_article import (
    STATUS_ERROR as ARTICLE_STATUS_ERROR,
)
from mtg_notion_manager.services.import_article import (
    build_article_import_plan,
    execute_article_import,
    write_article_import_log,
)
from mtg_notion_manager.services.import_cards import build_import_cards_plan, execute_import_cards
from mtg_notion_manager.services.import_deck import build_import_plan, execute_import
from mtg_notion_manager.services.review_duplicate_conflicts import (
    CATEGORY_INTENTIONAL,
    CATEGORY_MANUAL,
    CATEGORY_PRICE_ONLY,
    REVIEW_CATEGORY_LABELS,
    review_duplicate_conflicts,
    write_review_reports,
)
from mtg_notion_manager.services.title_update_dry_run import (
    ReadOnlyNotionClient,
    build_title_update_dry_run_plan,
    install_http_write_guard,
    load_confirmed_title_update_manifest,
    to_json_dict,
    write_json_report,
    write_markdown_report,
)
from mtg_notion_manager.services.verify_import import (
    VERIFICATION_VERIFIED,
    build_verify_import_plan,
    write_verify_report,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


def _english_name_from_pages(pages: list) -> Optional[str]:
    if not pages:
        return None
    prop = pages[0].get("properties", {}).get("英語名")
    if prop is None:
        return None
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", [])) or None


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
    confirmed_card_map: str = typer.Option(
        None,
        "--confirmed-card-map",
        help=(
            "記事から日本語名が取得できない新規カード(英語記事由来)について、"
            "人間が確認済みの英語名→日本語名対応を記したJSON設定のパス"
            "(config/confirmed_card_mapping.example.json 参照)。"
            "指定しない場合、そうしたカードは全て確認待ちとして新規作成をブロックする。"
        ),
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

            card_repo = CardRepository(
                client, config.card_data_source_id, overrides=load_card_match_overrides()
            )
            confirmed_mapping = None
            if confirmed_card_map:
                confirmed_mapping = load_confirmed_card_mapping(Path(confirmed_card_map), url)
            plan = build_import_cards_plan(
                url,
                resolved_deck_page_id,
                card_repo,
                deck_name=deck_name,
                allow_count_mismatch=allow_count_mismatch,
                confirmed_mapping=confirmed_mapping,
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
        intentional_duplicates = load_intentional_duplicates()
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        with NotionClient(config.notion_api_key) as client:
            repo = DedupeRepository(client, config.card_data_source_id)
            audits = audit_duplicate_groups(
                repo,
                card_name=card_name,
                exclusions=exclusions,
                intentional_duplicates=intentional_duplicates,
            )
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
    console.print(f"意図的に保持する重複: {counts[CATEGORY_INTENTIONAL_DUPLICATE]}グループ")

    intentional_audits = [a for a in audits if a.category == CATEGORY_INTENTIONAL_DUPLICATE]
    if intentional_audits:
        console.print()
        console.print("[bold]意図的に保持する重複の詳細[/bold]")
        for audit in intentional_audits:
            name_en = _english_name_from_pages(audit.pages)
            console.print(f"{audit.card_name} / {name_en or '(英語名不明)'}")
            console.print(f"  ページ数: {len(audit.pages)}")
            console.print(f"  理由: {audit.intentional_duplicate_reason}")
            console.print("  状態: intentional_duplicate")

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


@app.command(name="review-duplicate-conflicts")
def review_duplicate_conflicts_command(
    card_name: str = typer.Option(
        None, "--card-name", help="対象カード名を1件に絞り込む(省略時は要確認・手動指定の全件)"
    ),
    category: str = typer.Option(
        None,
        "--category",
        help=(
            "詳細分類で絞り込む "
            "(price-only / special-version / identity-conflict / other / manual-representative)"
        ),
    ),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="レポート(JSON/CSV/Markdown)の出力先ディレクトリ"
    ),
) -> None:
    """「要確認」グループをさらに詳細分類する(価格差異のみ/特殊仕様/同一性競合/その他/手動指定)。

    Notionへは一切書き込まない(読み取り専用)。
    """
    category_map = {
        "price-only": "price_only",
        "special-version": "special_version",
        "identity-conflict": "identity_conflict",
        "other": "other",
        "manual-representative": "manual_representative",
    }
    internal_category = None
    if category is not None:
        internal_category = category_map.get(category)
        if internal_category is None:
            console.print(
                f"[red]エラー:[/red] 不明な --category '{category}' です。"
                f" 指定可能な値: {', '.join(category_map)}"
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

    exclusions = load_exclusions()
    try:
        intentional_duplicates = load_intentional_duplicates()
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        with NotionClient(config.notion_api_key) as client:
            repo = DedupeRepository(client, config.card_data_source_id)
            reviews = review_duplicate_conflicts(
                repo,
                card_name=card_name,
                category=internal_category,
                exclusions=exclusions,
                intentional_duplicates=intentional_duplicates,
            )
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    paths = write_review_reports(reviews, Path(output_dir))

    regular_reviews = [r for r in reviews if r.review_category in REVIEW_CATEGORY_LABELS]
    intentional_reviews = [r for r in reviews if r.review_category == CATEGORY_INTENTIONAL]

    counts = {cat: 0 for cat in REVIEW_CATEGORY_LABELS}
    for review in regular_reviews:
        counts[review.review_category] += 1

    console.print(f"対象グループ数: {len(regular_reviews)}")
    for cat, label in REVIEW_CATEGORY_LABELS.items():
        console.print(f"{label}: {counts[cat]}")
    console.print(f"意図的に保持する重複: {len(intentional_reviews)}グループ")

    if intentional_reviews:
        console.print()
        console.print("[bold]意図的に保持する重複の詳細[/bold]")
        for review in intentional_reviews:
            name_en = _english_name_from_pages(review.pages)
            console.print(f"{review.card_name} / {name_en or '(英語名不明)'}")
            console.print(f"  ページ数: {len(review.pages)}")
            console.print(f"  理由: {review.intentional_duplicate_reason}")
            console.print("  状態: intentional_duplicate")
            console.print("  対応要否: 不要")

    console.print()
    console.print("レポートを出力しました:")
    console.print(f"  - {paths.json_path}")
    console.print(f"  - {paths.csv_path}")
    console.print(f"  - {paths.markdown_path}")


@app.command(name="apply-price-link-dedupe")
def apply_price_link_dedupe_command(
    targets_report: str = typer.Option(
        ..., "--targets-report", help="review-duplicate-conflicts が出力したJSONレポートのパス"
    ),
    scope: str = typer.Option(
        "remaining",
        "--scope",
        help=(
            "対象範囲(canary: カナリア3件 / remaining: カナリア以外のA分類 /"
            " manual: 手動代表指定グループ)"
        ),
    ),
    manual_representative_page_id: str = typer.Option(
        None,
        "--manual-representative-page-id",
        help="--scope manual のときに使う代表ページID(必須)",
    ),
    limit: int = typer.Option(
        None, "--limit", help="--scope remaining のとき、適用する最大グループ数"
    ),
    offset: int = typer.Option(0, "--offset", help="--scope remaining のとき、切り出し開始位置"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="計画の表示のみ行い、Notionへは書き込まない"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="実際にNotionへ統合を書き込む(指定しない限り書き込まない)"
    ),
    output_dir: str = typer.Option("reports", "--output-dir", help="実行ログの出力先ディレクトリ"),
) -> None:
    """review-duplicate-conflicts の price_only / manual_representative グループを段階適用する。

    適用直前に対象カード名を現在のNotion状態で再監査し、分類やページ構成が
    レポート作成時から変化していればそのグループをスキップする(削除APIは一切使用しない)。
    代表ページの販売価格・販売リンクは上書きせず、統合元の情報はメモへ履歴として追記する。
    """
    if scope not in ("canary", "remaining", "manual"):
        console.print(
            f"[red]エラー:[/red] 不明な --scope '{scope}' です。"
            " canary/remaining/manual のいずれかを指定してください。"
        )
        raise typer.Exit(code=1)

    if scope == "manual" and not manual_representative_page_id:
        console.print(
            "[red]エラー:[/red] --scope manual では --manual-representative-page-id が必須です。"
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
        report_path = Path(targets_report)
        overrides = {}
        if manual_representative_page_id:
            all_targets_preview = load_price_link_targets(report_path)
            manual_names = [
                t.card_name for t in all_targets_preview if t.review_category == CATEGORY_MANUAL
            ]
            overrides = {name: manual_representative_page_id for name in manual_names}
        all_targets = load_price_link_targets(
            report_path, manual_representative_overrides=overrides
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]エラー:[/red] レポートを読み込めませんでした: {exc}")
        raise typer.Exit(code=1) from exc

    price_only_targets = [t for t in all_targets if t.review_category == CATEGORY_PRICE_ONLY]

    if scope == "canary":
        targets = select_canary_targets(price_only_targets, limit=3)
    elif scope == "manual":
        targets = [t for t in all_targets if t.review_category == CATEGORY_MANUAL]
    else:
        canary = select_canary_targets(price_only_targets, limit=3)
        canary_names = {t.card_name for t in canary}
        remaining = [t for t in price_only_targets if t.card_name not in canary_names]
        targets = select_remaining_batch(remaining, limit=limit, offset=offset)

    console.print(f"対象グループ数: {len(targets)}")
    table = Table(title=f"適用対象(scope={scope})")
    table.add_column("カード名")
    table.add_column("分類")
    table.add_column("重複件数", justify="right")
    table.add_column("代表ページID(手動指定)")
    for group in targets:
        table.add_row(
            group.card_name,
            group.review_category,
            str(len(group.page_ids)),
            group.representative_page_id or "",
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
            outcomes = apply_price_link_targets(
                repo, targets, apply=do_apply, exclusions=exclusions
            )
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
        PRICE_STATUS_APPLIED: 0,
        PRICE_STATUS_PLANNED: 0,
        PRICE_STATUS_SKIPPED_STALE: 0,
        PRICE_STATUS_SKIPPED_NOT_DUPLICATE: 0,
        PRICE_STATUS_FAILED: 0,
    }
    for outcome in outcomes:
        counts[outcome.status] += 1

    console.print()
    console.print(f"適用: {counts[PRICE_STATUS_APPLIED]}件")
    console.print(f"計画のみ(dry-run): {counts[PRICE_STATUS_PLANNED]}件")
    console.print(f"スキップ(鮮度不一致): {counts[PRICE_STATUS_SKIPPED_STALE]}件")
    console.print(f"スキップ(重複解消済み): {counts[PRICE_STATUS_SKIPPED_NOT_DUPLICATE]}件")
    console.print(f"失敗: {counts[PRICE_STATUS_FAILED]}件")

    log_paths = write_price_link_apply_log(
        outcomes, targets_report_path=targets_report, output_dir=Path(output_dir), applied=do_apply
    )
    console.print()
    console.print(f"実行ログ: {log_paths.json_path}")

    if not do_apply:
        console.print(
            "[cyan]--apply が指定されていないため、Notionへの書き込みは行いません。[/cyan]"
        )
        raise typer.Exit(code=0)

    if counts[PRICE_STATUS_FAILED]:
        raise typer.Exit(code=1)


@app.command(name="import-article")
def import_article_command(
    url: str = typer.Argument(..., help="複数デッキを含む統率者デッキ紹介記事のURL"),
    exclude_deck: list[str] = typer.Option(
        None, "--exclude-deck", help="処理から除外するデッキ名(複数指定可)"
    ),
    include_deck: list[str] = typer.Option(
        None, "--include-deck", help="このデッキ名だけを処理対象にする(複数指定可)"
    ),
    deck_page_map: str = typer.Option(
        None,
        "--deck-page-map",
        help=(
            "記事内デッキ名と既存Notionページの明示的な対応を記したJSON設定のパス"
            "(config/deck_page_mapping.example.json 参照)。"
            "記事側のdeck-title属性がNotionページ名と完全一致しない場合に使う。"
            "自動翻訳・類似一致は行わず、指定page_id・対象記事・期待ページ名を検証する。"
        ),
    ),
    confirmed_card_map: str = typer.Option(
        None,
        "--confirmed-card-map",
        help=(
            "記事から日本語名が取得できない新規カード(英語記事由来)について、"
            "人間が確認済みの英語名→日本語名対応を記したJSON設定のパス"
            "(config/confirmed_card_mapping.example.json 参照)。"
            "指定しない場合、そうしたカードは全て確認待ちとして新規作成をブロックする"
            "(未確認の英語名を日本語タイトルへ書き込むことは一切ない)。"
        ),
    ),
    pending_manifest_output: str = typer.Option(
        None,
        "--pending-manifest-output",
        help="確認待ち(および作成可能)な新規カードの一覧をJSONで出力するパス",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="取得・解析・照合・差分表示のみ行い、Notionへは書き込まない"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="実際にNotionへ書き込む(指定しない限り書き込まない)"
    ),
    show_detail: bool = typer.Option(
        False, "--detail/--no-detail", help="デッキごとのカード別詳細テーブルも表示する"
    ),
    output_dir: str = typer.Option("reports", "--output-dir", help="実行ログの出力先ディレクトリ"),
) -> None:
    """記事内の複数統率者デッキのカード一式を、まとめてMTGカードDBへ登録する。

    各デッキはMTG統率者DBの既存デッキと完全一致で照合する(一致しないデッキは新規作成せず
    要確認として扱う)。カードDBは記事全体で1回だけ取得して索引化し、全デッキで共有する。
    新規カードは、記事から取得済みの日本語名(mtg-jp.com等)または --confirmed-card-map で
    人間確認済みの日本語名がある場合のみ作成可能になる。今回の対象範囲(全デッキ・全カード)の
    計画が全件成功する場合のみ書き込みフェーズを開始し、1件でも曖昧一致・未解決・確認待ちの
    カードがあれば対象範囲全体でNotionへの書き込みを一切行わない
    (デッキ単位でplanとwriteを交互に行わない安全機構)。削除APIは一切使用しない。
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=1)

    do_apply = apply and not dry_run

    try:
        with NotionClient(config.notion_api_key) as client:
            writer = NotionWriter(client, config.commander_data_source_id)
            card_repo = CardRepository(
                client, config.card_data_source_id, overrides=load_card_match_overrides()
            )

            plan = build_article_import_plan(
                url,
                writer,
                card_repo,
                exclude_deck_names=exclude_deck or [],
                include_deck_names=include_deck or [],
                deck_page_map_path=Path(deck_page_map) if deck_page_map else None,
                confirmed_card_map_path=Path(confirmed_card_map) if confirmed_card_map else None,
            )

            print_article_plan_summary(console, plan)
            console.print()
            if show_detail:
                print_article_deck_detail(console, plan)

            if pending_manifest_output:
                if plan.pending_manifest is not None:
                    write_pending_manifest(plan.pending_manifest, Path(pending_manifest_output))
                    console.print(f"確認待ちマニフェスト: {pending_manifest_output}")
                else:
                    console.print("新規カードが無いため、マニフェストは出力しませんでした。")

            if do_apply:
                plan = execute_article_import(plan, card_repo, note="import-article由来")
                console.print()
                print_article_apply_result(console, plan)
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    log_paths = write_article_import_log(plan, output_dir=Path(output_dir), applied=do_apply)
    console.print()
    console.print(f"実行ログ: {log_paths.json_path}")

    if not do_apply:
        console.print(
            "[cyan]--apply が指定されていないため、Notionへの書き込みは行いません。[/cyan]"
        )
        raise typer.Exit(code=0)

    counts = plan.counts
    has_apply_failures = any(
        entry.apply_result is not None and entry.apply_result.failed for entry in plan.entries
    )
    if counts[ARTICLE_STATUS_ERROR] or has_apply_failures:
        raise typer.Exit(code=1)


@app.command(name="verify-import")
def verify_import_command(
    url: str = typer.Argument(..., help="検証対象の統率者デッキ紹介記事のURL"),
    include_deck: list[str] = typer.Option(
        None, "--include-deck", help="このデッキ名だけを検証対象にする(複数指定可)"
    ),
    deck_page_map: str = typer.Option(
        None,
        "--deck-page-map",
        help=(
            "記事内デッキ名と既存Notionページの明示的な対応を記したJSON設定のパス"
            "(config/deck_page_mapping.example.json 参照)。"
            "記事側のdeck-title属性がNotionページ名と完全一致しない場合に使う。"
            "自動翻訳・類似一致は行わず、指定page_id・対象記事・期待ページ名を検証する。"
        ),
    ),
    confirmed_card_map: str = typer.Option(
        None,
        "--confirmed-card-map",
        help=(
            "人間確認済みの英語名→日本語名対応を記したJSON設定のパス"
            "(config/confirmed_card_mapping.example.json 参照)。"
            " --confirmed-card-map は import-article と同じ設定ファイル・同じresolverを使う"
            "(新規カードのprovenance判定が両コマンドで食い違うことはない)。"
        ),
    ),
    show_detail: bool = typer.Option(
        False, "--detail/--no-detail", help="デッキごとの差分カード詳細も表示する"
    ),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="検証レポートの出力先ディレクトリ"
    ),
) -> None:
    """記事から抽出できるカード・relationが、Notion上に既に正しく登録されているかを検証する。

    import-articleの取り込み前チェック(--dry-run)とは異なり、取り込み済みのはずの
    デッキが実際にその通りに登録されているかを確認する読み取り専用コマンド
    (database query / page retrieve / relation property read のみ行う。
    Notionへの書き込みは一切行わず、--applyに相当するオプションも存在しない)。

    --deck-page-map は import-article と同じ設定ファイル・同じresolverを使う
    (デッキページの解決方法が両コマンドで食い違うことはない)。

    終了コード: 0=全デッキ検証成功 / 1=登録状態に差分あり / 2=入力・設定・記事取得・
    Notion読取などの実行エラー。
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=2)

    try:
        with NotionClient(config.notion_api_key) as client:
            writer = NotionWriter(client, config.commander_data_source_id)
            card_repo = CardRepository(
                client, config.card_data_source_id, overrides=load_card_match_overrides()
            )

            report = build_verify_import_plan(
                url,
                client,
                writer,
                card_repo,
                include_deck_names=include_deck or [],
                deck_page_map_path=Path(deck_page_map) if deck_page_map else None,
                confirmed_card_map_path=Path(confirmed_card_map) if confirmed_card_map else None,
            )
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    print_verify_import_summary(console, report)
    console.print()
    if show_detail:
        print_verify_import_detail(console, report)

    paths = write_verify_report(report, output_dir=Path(output_dir))
    console.print("レポートを出力しました:")
    console.print(f"  - {paths.json_path}")

    if report.verification_status != VERIFICATION_VERIFIED:
        raise typer.Exit(code=1)


@app.command(name="plan-title-updates")
def plan_title_updates_command(
    manifest: str = typer.Option(
        ..., "--manifest", help="人間確認済みタイトル更新マニフェストのJSONパス"
    ),
    expected_count: int = typer.Option(
        ...,
        "--expected-count",
        help="マニフェストに含まれるべきentry件数(不一致ならNotionへ接続せず失敗する)",
    ),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="dry-runレポート(JSON/Markdown)の出力先ディレクトリ"
    ),
) -> None:
    """人間確認済みの日本語タイトルへの変更計画を、読み取り専用で作成する(dry-run専用)。

    Notionへの書き込みは一切行わない(--applyに相当するオプションは存在しない)。
    対象ページはマニフェスト内のpage_idのみで特定する(英語名・日本語名の検索は
    同名ページの衝突確認にのみ使い、対象の決定には使わない)。

    終了コード: 0=対象件数が期待値と一致し全件適用可能 / 1=マニフェスト不正・
    件数不一致・Notion読み取りエラー・1件でも適用不可(ブロック)のいずれか。
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not config.card_data_source_id:
        console.print("[red]設定エラー:[/red] NOTION_CARD_DATA_SOURCE_ID が設定されていません。")
        raise typer.Exit(code=1)

    manifest_path = Path(manifest)
    try:
        loaded_manifest = load_confirmed_title_update_manifest(
            manifest_path, expected_entry_count=expected_count
        )
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        with NotionClient(config.notion_api_key) as client:
            http_call_log = install_http_write_guard(client)
            read_only_client = ReadOnlyNotionClient(client)
            report = build_title_update_dry_run_plan(
                read_only_client,
                config.card_data_source_id,
                loaded_manifest,
                str(manifest_path),
            )
    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    write_ops = sum(1 for call in http_call_log if not call.allowed)
    data = to_json_dict(report, write_operations=write_ops, write_attempts=write_ops)
    data["expected_target_count"] = expected_count
    data["notion_access"]["called_endpoints"] = sorted(
        {f"{c.method} {c.url}" for c in http_call_log}
    )
    data["notion_access"]["rejected_call_count"] = write_ops

    console.print(
        f"対象件数: {len(report.entries)} / 期待件数: {expected_count}"
    )
    console.print(f"適用可能: {report.eligible_count} / ブロック: {report.blocked_count}")
    console.print(f"all-or-nothing判定: {report.all_or_nothing_eligible}")
    console.print(f"Notion書き込み操作数: {write_ops} / 拒否されたHTTP呼び出し: {write_ops}")

    table = Table(title="タイトル更新dry-run結果")
    table.add_column("page_id")
    table.add_column("現在タイトル")
    table.add_column("新タイトル")
    table.add_column("適用可能")
    table.add_column("ブロック理由")
    for entry in report.entries:
        table.add_row(
            entry.page_id,
            entry.current_title or "-",
            entry.confirmed_new_title,
            str(entry.eligible_for_future_update),
            "; ".join(entry.blocking_reasons) or "-",
        )
    console.print(table)

    json_path = write_json_report(
        data, Path(output_dir) / f"dry-run-card-title-updates-{_timestamp()}.json"
    )
    md_path = write_markdown_report(
        data, Path(output_dir) / f"dry-run-card-title-updates-{_timestamp()}.md"
    )
    console.print("レポートを出力しました:")
    console.print(f"  - {json_path}")
    console.print(f"  - {md_path}")
    console.print(
        "[cyan]このコマンドは読み取り専用のdry-run専用です。"
        "Notionへの書き込みは一切行いません。[/cyan]"
    )

    if not report.all_or_nothing_eligible:
        raise typer.Exit(code=1)


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


if __name__ == "__main__":
    app()

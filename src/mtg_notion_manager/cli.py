import json

import typer
from rich.console import Console
from rich.table import Table

from mtg_notion_manager.config import Config, ConfigError
from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.services.import_deck import build_import_plan, execute_import

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.callback()
def main() -> None:
    """MTG統率者デッキをNotionで管理するCLIツール。"""


@app.command(name="import")
def import_command(
    url: str = typer.Argument(
        ..., help="統率者デッキ紹介ページのURL(magic.wizards.com または mtg-jp.com)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Notionへは書き込まず、プレビューのみ表示する"
    ),
) -> None:
    """デッキ情報を取得してNotionのMTG統率者DBに登録する。"""
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=1)

    try:
        with NotionClient(config.notion_api_key) as client:
            writer = NotionWriter(client, config.commander_data_source_id)
            plan = build_import_plan(url, writer)

            console.print("[bold]プレビュー[/bold]")
            console.print_json(json.dumps(plan.record.to_preview_dict(), ensure_ascii=False))

            if plan.is_duplicate:
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
                console.print(
                    "[cyan]--dry-run のためNotionへの書き込みは行いません。[/cyan]"
                )
                raise typer.Exit(code=0)

            if not typer.confirm("この内容でNotionに登録しますか?"):
                console.print("キャンセルしました。")
                raise typer.Exit(code=0)

            execute_import(plan, writer)
            console.print("[green]Notionに登録しました。[/green]")

    except MtgNotionManagerError as exc:
        console.print(f"[red]エラー:[/red] {exc}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

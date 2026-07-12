from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.services.review_duplicate_conflicts import (
    CATEGORY_PRICE_ONLY,
    DetailedGroupReview,
    ReviewReportPaths,
)

runner = CliRunner()


def _fake_config() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id="card-ds-id",
    )


def _fake_config_without_card_db() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id=None,
    )


class FakeNotionClientCtx:
    def __enter__(self) -> FakeNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _sample_reviews() -> list[DetailedGroupReview]:
    return [
        DetailedGroupReview(
            card_name="沼",
            pages=[{"id": "p1"}, {"id": "p2"}],
            review_category=CATEGORY_PRICE_ONLY,
            representative_candidate_id="p1",
            representative_reasons=["英語名あり"],
            prices=[100, 200],
            links=[],
            conflicts=[],
            role_conflict=False,
            special_flags=[],
            merged_deck_relation_count=1,
            merged_commander_tags=[],
            estimated_quantity=2,
            recommended_price_link_handling="3案を比較",
            integrable=True,
            risks=[],
        )
    ]


def _patch_common(monkeypatch: pytest.MonkeyPatch, client: object | None = None) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: client or FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())


def test_writes_reports_and_prints_category_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)

    captured: dict[str, object] = {}

    def fake_review(
        repo: object,
        card_name: str | None = None,
        category: str | None = None,
        exclusions: object = None,
    ) -> list:
        captured["card_name"] = card_name
        captured["category"] = category
        return _sample_reviews()

    monkeypatch.setattr(cli, "review_duplicate_conflicts", fake_review)

    write_calls: list[tuple] = []

    def fake_write(
        reviews: list, output_dir: Path, timestamp: str | None = None
    ) -> ReviewReportPaths:
        write_calls.append((reviews, output_dir))
        return ReviewReportPaths(
            json_path=output_dir / "r.json",
            csv_path=output_dir / "r.csv",
            markdown_path=output_dir / "r.md",
        )

    monkeypatch.setattr(cli, "write_review_reports", fake_write)

    result = runner.invoke(
        cli.app, ["review-duplicate-conflicts", "--output-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert len(write_calls) == 1
    assert captured["card_name"] is None
    assert captured["category"] is None
    assert "対象グループ数: 1" in result.stdout
    assert "A: 価格・販売リンク差異のみ: 1" in result.stdout


def test_card_name_filter_is_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)

    captured: dict[str, object] = {}

    def fake_review(
        repo: object,
        card_name: str | None = None,
        category: str | None = None,
        exclusions: object = None,
    ) -> list:
        captured["card_name"] = card_name
        return []

    monkeypatch.setattr(cli, "review_duplicate_conflicts", fake_review)
    monkeypatch.setattr(
        cli,
        "write_review_reports",
        lambda reviews, output_dir, timestamp=None: ReviewReportPaths(
            json_path=output_dir / "r.json",
            csv_path=output_dir / "r.csv",
            markdown_path=output_dir / "r.md",
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "review-duplicate-conflicts",
            "--card-name",
            "血染めのぬかるみ",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["card_name"] == "血染めのぬかるみ"


def test_category_option_maps_to_internal_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)

    captured: dict[str, object] = {}

    def fake_review(
        repo: object,
        card_name: str | None = None,
        category: str | None = None,
        exclusions: object = None,
    ) -> list:
        captured["category"] = category
        return []

    monkeypatch.setattr(cli, "review_duplicate_conflicts", fake_review)
    monkeypatch.setattr(
        cli,
        "write_review_reports",
        lambda reviews, output_dir, timestamp=None: ReviewReportPaths(
            json_path=output_dir / "r.json",
            csv_path=output_dir / "r.csv",
            markdown_path=output_dir / "r.md",
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "review-duplicate-conflicts",
            "--category",
            "price-only",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["category"] == "price_only"


def test_unknown_category_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_common(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "review-duplicate-conflicts",
            "--category",
            "not-a-real-category",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1


def test_missing_card_data_source_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, ["review-duplicate-conflicts"])

    assert result.exit_code == 1


def test_command_never_touches_notion_client_write_methods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLIレベルでも、Notionクライアントの更新系メソッドが呼ばれる余地がないことを確認する。"""

    class StrictFakeClient:
        def __enter__(self) -> StrictFakeClient:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def update_page(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("update_page must not be called by review-duplicate-conflicts")

        def update_data_source_schema(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("update_data_source_schema must not be called")

    _patch_common(monkeypatch, client=StrictFakeClient())
    monkeypatch.setattr(
        cli,
        "review_duplicate_conflicts",
        lambda repo, card_name=None, category=None, exclusions=None: [],
    )
    monkeypatch.setattr(
        cli,
        "write_review_reports",
        lambda reviews, output_dir, timestamp=None: ReviewReportPaths(
            json_path=output_dir / "r.json",
            csv_path=output_dir / "r.csv",
            markdown_path=output_dir / "r.md",
        ),
    )

    result = runner.invoke(
        cli.app, ["review-duplicate-conflicts", "--output-dir", str(tmp_path)]
    )

    assert result.exit_code == 0

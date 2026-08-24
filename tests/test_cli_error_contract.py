"""CLI_ERROR_CONTRACT v1(--error-json)のpilot(import-article / apply-single-title-update)テスト。

Notion/外部サイトへは一切接続しない(すべてfake/monkeypatch)。
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.error_contract import SCHEMA_VERSION, ErrorCategory, ErrorCode
from mtg_notion_manager.exceptions import (
    MappingError,
    MultipleDecksFoundError,
    NotionAPIError,
)
from mtg_notion_manager.models import DeckCard, ParsedDeckList
from mtg_notion_manager.services.import_article import ArticleImportLogPaths, ArticleImportPlan
from mtg_notion_manager.services.import_cards import ImportCardsPlan
from mtg_notion_manager.services.single_card_title_update import (
    GuardedHttpCallRecord,
    SingleUpdatePreflightResult,
)
from mtg_notion_manager.services.title_update_dry_run import ConfirmedTitleUpdateEntry

runner = CliRunner()
URL = "https://magic.wizards.com/ja/news/announcements/secrets-of-strixhaven-commander-decklists"

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_REQUIRED_JSON_KEYS = {"schema_version", "command", "error_category", "error_code", "message"}
_PROHIBITED_JSON_KEYS = {
    "ok",
    "details",
    "exception_type",
    "retryable",
    "mutation_state",
    "mutation_outcome",
    "traceback",
}


def _assert_pure_json_error(stdout: str, *, command: str, category: str, code: str) -> dict:
    """stdout全体が純粋な1個のJSONオブジェクトであることを確認する(§5 JSON純度)。"""
    assert "\x1b" not in stdout, "stdoutにANSIエスケープが含まれてはならない"
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert _REQUIRED_JSON_KEYS <= payload.keys()
    assert not (_PROHIBITED_JSON_KEYS & payload.keys())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == command
    assert payload["error_category"] == category
    assert payload["error_code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    return payload


def _fake_config() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id="card-ds-id",
    )


class FakeNotionClientCtx:
    def __enter__(self) -> FakeNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(
        cli, "CardRepository", lambda client, data_source_id, overrides=None: object()
    )
    monkeypatch.setattr(cli, "NotionWriter", lambda client, data_source_id: object())


def _card(name_en: str) -> DeckCard:
    return DeckCard(name_ja=None, name_en=name_en, quantity=1, is_commander=False, source_url=URL)


def _sample_plan() -> ArticleImportPlan:
    from mtg_notion_manager.services.import_article import DeckArticleEntry

    card = _card("sample-card")
    parsed = ParsedDeckList(
        deck_name="デッキA", commander_name=card.display_name, cards=[card], source_url=URL
    )
    cards_plan = ImportCardsPlan(parsed=parsed, deck_page_id="deck-デッキA", decisions=[])
    entry = DeckArticleEntry(
        deck_name="デッキA",
        status="ready",
        deck_page_id="deck-デッキA",
        deck_page_url="https://notion.so/deck-デッキA",
        cards_plan=cards_plan,
    )
    return ArticleImportPlan(
        source_url=URL, all_deck_names=["デッキA"], excluded_deck_names=[], entries=[entry]
    )


def _patch_write_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "write_article_import_log",
        lambda plan, output_dir, applied, timestamp=None: ArticleImportLogPaths(
            json_path=output_dir / "log.json"
        ),
    )


# --- import-article ----------------------------------------------------------


def test_import_article_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["import-article", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_import_article_human_error_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise MappingError("セット名 'SPM' はマッピングできません。")

    monkeypatch.setattr(cli, "build_article_import_plan", _raise)

    result = runner.invoke(cli.app, ["import-article", URL, "--dry-run"])

    assert result.exit_code == 1
    assert "エラー" in result.stdout
    assert "SPM" in result.stdout
    # 人間モードではJSONは出力されない。
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_import_article_error_json_mapping_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise MappingError("セット名 'SPM' はマッピングできません。")

    monkeypatch.setattr(cli, "build_article_import_plan", _raise)

    result = runner.invoke(cli.app, ["import-article", URL, "--dry-run", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-article",
        category=ErrorCategory.MAPPING,
        code=ErrorCode.UNMAPPED_VALUE,
    )


def test_import_article_error_json_multiple_decks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise MultipleDecksFoundError("複数デッキが検出されました。")

    monkeypatch.setattr(cli, "build_article_import_plan", _raise)

    result = runner.invoke(cli.app, ["import-article", URL, "--dry-run", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-article",
        category=ErrorCategory.IDENTITY_AMBIGUITY,
        code=ErrorCode.MULTIPLE_DECKS_FOUND,
    )


def test_import_article_error_json_exit_code_matches_human_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise MappingError("マッピング不能")

    monkeypatch.setattr(cli, "build_article_import_plan", _raise)

    human = runner.invoke(cli.app, ["import-article", URL, "--dry-run"])
    structured = runner.invoke(cli.app, ["import-article", URL, "--dry-run", "--error-json"])

    assert human.exit_code == structured.exit_code == 1


def test_import_article_error_json_success_output_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    monkeypatch.setattr(cli, "build_article_import_plan", lambda *a, **k: _sample_plan())

    human = runner.invoke(cli.app, ["import-article", URL, "--dry-run"])
    structured = runner.invoke(cli.app, ["import-article", URL, "--dry-run", "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    # 成功時にはJSONスキーマが存在しないため、stdout全体はJSONとして解釈できない。
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_article_error_json_purity_after_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plan概要が既に表示された後、--applyの書き込みで失敗するケースでも
    stdoutが純粋なJSON1件だけになること(バッファリングによるstdout純度維持)を確認する。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    monkeypatch.setattr(cli, "build_article_import_plan", lambda *a, **k: _sample_plan())

    def _raise_on_apply(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise NotionAPIError("Notion APIへの接続がタイムアウトしました。")

    monkeypatch.setattr(cli, "execute_article_import", _raise_on_apply)

    result = runner.invoke(cli.app, ["import-article", URL, "--apply", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-article",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


# --- apply-single-title-update ------------------------------------------------

FAKE_PAGE_ID = "00000000-0000-0000-0000-000000000001"
FAKE_DECK_ID = "00000000-0000-0000-0000-000000000002"


def _fake_entry(**overrides: object) -> ConfirmedTitleUpdateEntry:
    base: dict[str, object] = dict(
        page_id=FAKE_PAGE_ID,
        expected_current_title="Elusive Otter",
        confirmed_new_title="神出鬼没のカワウソ",
        expected_english_name="Elusive Otter",
        source_deck_ids=[FAKE_DECK_ID],
        verification_status="human_confirmed",
        verification_actor="user",
        verification_note="note",
    )
    base.update(overrides)
    return ConfirmedTitleUpdateEntry(**base)  # type: ignore[arg-type]


def _fake_preflight(**overrides: object) -> SingleUpdatePreflightResult:
    base: dict[str, object] = dict(
        page_id=FAKE_PAGE_ID,
        title_property_name="カード名",
        current_title="Elusive Otter",
        expected_current_title="Elusive Otter",
        confirmed_new_title="神出鬼没のカワウソ",
        current_english_name="Elusive Otter",
        expected_english_name="Elusive Otter",
        is_archived_or_trashed=False,
        last_edited_time="2026-01-01T00:00:00.000Z",
        same_title_check=None,
        relation_snapshot=None,
        current_title_matches=True,
        english_name_matches=True,
        eligible_for_future_update=True,
        blocking_reasons=[],
        operation_digest="fixed-test-digest",
    )
    base.update(overrides)
    return SingleUpdatePreflightResult(**base)  # type: ignore[arg-type]


def _patch_single_update_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "install_http_write_guard", lambda client: [])
    monkeypatch.setattr(cli, "ReadOnlyNotionClient", lambda client: object())
    monkeypatch.setattr(cli, "load_single_update_manifest", lambda path: _fake_entry())
    monkeypatch.setattr(
        cli,
        "preflight_to_json_dict",
        lambda preflight, write_operations: {"write_operations": write_operations},
    )
    monkeypatch.setattr(cli, "write_single_json_report", lambda data, path: path)
    monkeypatch.setattr(cli, "write_single_markdown_report", lambda data, path: path)


_BASE_ARGS = [
    "apply-single-title-update",
    "--manifest",
    "manifest.json",
    "--expected-count",
    "1",
    "--max-updates",
    "1",
]


def test_apply_single_title_update_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["apply-single-title-update", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_apply_single_title_update_expected_count_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    human = runner.invoke(
        cli.app,
        [
            "apply-single-title-update",
            "--manifest",
            "manifest.json",
            "--expected-count",
            "2",
            "--max-updates",
            "1",
        ],
    )
    structured = runner.invoke(
        cli.app,
        [
            "apply-single-title-update",
            "--manifest",
            "manifest.json",
            "--expected-count",
            "2",
            "--max-updates",
            "1",
            "--error-json",
        ],
    )

    assert human.exit_code == structured.exit_code == 1
    _assert_pure_json_error(
        structured.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.INPUT_VALIDATION,
        code=ErrorCode.EXPECTED_COUNT_NOT_ONE,
    )


def test_apply_single_title_update_max_updates_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(
        cli.app,
        [
            "apply-single-title-update",
            "--manifest",
            "manifest.json",
            "--expected-count",
            "1",
            "--max-updates",
            "2",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.INPUT_VALIDATION,
        code=ErrorCode.MAX_UPDATES_NOT_ONE,
    )


def test_apply_single_title_update_preflight_not_eligible_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """preflight不適用の場合、その手前で表示されるはずのpreflight詳細・レポート
    出力メッセージが一切stdoutへ混入せず、純粋なJSON1件だけになることを確認する
    (--apply を付けない、dry preflightのみのケース)。
    """
    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_single_update_preflight",
        lambda client, ds_id, entry: _fake_preflight(
            eligible_for_future_update=False, blocking_reasons=["page_is_archived_or_trashed"]
        ),
    )

    result = runner.invoke(cli.app, [*_BASE_ARGS, "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.PRECONDITION,
        code=ErrorCode.PREFLIGHT_NOT_ELIGIBLE,
    )


def test_apply_single_title_update_preflight_not_eligible_human_and_json_exit_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_single_update_preflight",
        lambda client, ds_id, entry: _fake_preflight(
            eligible_for_future_update=False, blocking_reasons=["page_is_archived_or_trashed"]
        ),
    )

    human = runner.invoke(cli.app, _BASE_ARGS)
    structured = runner.invoke(cli.app, [*_BASE_ARGS, "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    assert "page_is_archived_or_trashed" not in structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)


def test_apply_single_title_update_dry_preflight_eligible_success_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--apply を付けない、eligible=Trueのdry preflightは成功(exit=0)であり、
    --error-json有無で出力が変わらないことを確認する(成功時はJSON化しない)。
    """
    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli, "build_single_update_preflight", lambda client, ds_id, entry: _fake_preflight()
    )

    human = runner.invoke(cli.app, _BASE_ARGS)
    structured = runner.invoke(cli.app, [*_BASE_ARGS, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert "fixed-test-digest" in structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_apply_single_title_update_approval_digest_mismatch_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eligible=Trueのためpreflight詳細・レポート出力は一度バッファへ書かれるが、
    approval-digest不一致で失敗する場合、それらがstdoutへ漏れず純粋なJSONになることを
    確認する(バッファリングによる後段purity fixの検証)。
    """
    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli, "build_single_update_preflight", lambda client, ds_id, entry: _fake_preflight()
    )

    result = runner.invoke(
        cli.app,
        [*_BASE_ARGS, "--apply", "--approval-digest", "wrong-digest", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.PRECONDITION,
        code=ErrorCode.APPROVAL_DIGEST_MISMATCH,
    )
    assert "fixed-test-digest" not in result.stdout


def test_apply_single_title_update_optimistic_lock_mismatch_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_update_common(monkeypatch)
    calls = {"count": 0}

    def _fake_build(client: object, ds_id: object, entry: object) -> SingleUpdatePreflightResult:
        calls["count"] += 1
        if calls["count"] == 1:
            return _fake_preflight()
        return _fake_preflight(current_title="Someone Else Edited This")

    monkeypatch.setattr(cli, "build_single_update_preflight", _fake_build)

    result = runner.invoke(
        cli.app,
        [*_BASE_ARGS, "--apply", "--approval-digest", "fixed-test-digest", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.PRECONDITION,
        code=ErrorCode.OPTIMISTIC_LOCK_MISMATCH,
    )


def test_apply_single_title_update_notion_api_error_on_write_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli, "build_single_update_preflight", lambda client, ds_id, entry: _fake_preflight()
    )
    monkeypatch.setattr(cli, "install_single_title_update_write_guard", lambda client, **kw: [])

    class _RaisingWriter:
        def __init__(self, client: object) -> None:
            pass

        def update_title(self, page_id: str, prop: str, title: str) -> None:
            raise NotionAPIError("Notion APIへの接続に失敗しました。")

    monkeypatch.setattr(cli, "SingleTitleUpdateWriter", _RaisingWriter)

    result = runner.invoke(
        cli.app,
        [*_BASE_ARGS, "--apply", "--approval-digest", "fixed-test-digest", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


def test_apply_single_title_update_post_verification_failed_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relation_snapshot=None のfixtureでは verify_post_update が
    source_deck_relation_maintained=False を返すため、事後検証が確実に失敗する
    (実際のservice関数をそのまま使い、モックしない)。
    """
    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli, "build_single_update_preflight", lambda client, ds_id, entry: _fake_preflight()
    )
    monkeypatch.setattr(cli, "install_single_title_update_write_guard", lambda client, **kw: [])

    class _NoopWriter:
        def __init__(self, client: object) -> None:
            pass

        def update_title(self, page_id: str, prop: str, title: str) -> None:
            return None

    monkeypatch.setattr(cli, "SingleTitleUpdateWriter", _NoopWriter)

    result = runner.invoke(
        cli.app,
        [*_BASE_ARGS, "--apply", "--approval-digest", "fixed-test-digest", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.INTEGRITY,
        code=ErrorCode.POST_VERIFICATION_FAILED,
    )
    # 書き込み実行より前に表示されるはずだったpreflight詳細もバッファされ、漏れない。
    assert "fixed-test-digest" not in result.stdout


def test_apply_single_title_update_missing_card_data_source_id_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_config_without_card_db() -> Config:
        return Config(
            notion_api_key="secret_test", commander_data_source_id="commander-ds-id",
            card_data_source_id=None,
        )

    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, [*_BASE_ARGS, "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_apply_single_title_update_manifest_load_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtg_notion_manager.services.title_update_dry_run import TitleUpdateManifestConfigError

    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    def _raise(path: object) -> ConfirmedTitleUpdateEntry:
        raise TitleUpdateManifestConfigError("マニフェストが不正です。")

    monkeypatch.setattr(cli, "load_single_update_manifest", _raise)

    result = runner.invoke(cli.app, [*_BASE_ARGS, "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.MANIFEST_INVALID,
    )


def test_apply_single_title_update_write_guard_rejected_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtg_notion_manager.services.single_card_title_update import SingleUpdateGuardError

    _patch_single_update_common(monkeypatch)
    monkeypatch.setattr(
        cli, "build_single_update_preflight", lambda client, ds_id, entry: _fake_preflight()
    )

    def _raise_guard(client: object, **kwargs: object) -> list[GuardedHttpCallRecord]:
        raise SingleUpdateGuardError("承認されていない書き込みリクエストが検出されました。")

    monkeypatch.setattr(cli, "install_single_title_update_write_guard", _raise_guard)

    result = runner.invoke(
        cli.app,
        [*_BASE_ARGS, "--apply", "--approval-digest", "fixed-test-digest", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-single-title-update",
        category=ErrorCategory.INTEGRITY,
        code=ErrorCode.WRITE_GUARD_REJECTED,
    )


# --- 汎用契約テスト ------------------------------------------------------------


def test_mapping_error_does_not_fall_back_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise MappingError("明示的にマッピングされている例外")

    monkeypatch.setattr(cli, "build_article_import_plan", _raise)

    result = runner.invoke(cli.app, ["import-article", URL, "--dry-run", "--error-json"])

    # MappingErrorは明示的にマッピングされているため UNMAPPED_VALUE になる
    # (INTERNAL/UNCLASSIFIED_DOMAIN_ERRORにフォールバックしないことの回帰確認)。
    payload = json.loads(result.stdout)
    assert payload["error_category"] == ErrorCategory.MAPPING
    assert payload["error_code"] == ErrorCode.UNMAPPED_VALUE


def test_import_article_unhandled_exception_falls_back_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MtgNotionManagerErrorの階層に属さない、真に想定外の例外のケース(§15/§37)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleImportPlan:
        raise ValueError("想定外の内部エラー")

    monkeypatch.setattr(cli, "build_article_import_plan", _raise)

    structured = runner.invoke(cli.app, ["import-article", URL, "--dry-run", "--error-json"])
    human = runner.invoke(cli.app, ["import-article", URL, "--dry-run"])

    assert structured.exit_code == 1
    _assert_pure_json_error(
        structured.stdout,
        command="import-article",
        category=ErrorCategory.INTERNAL,
        code=ErrorCode.UNHANDLED_EXCEPTION,
    )
    # human modeでは既存動作(例外がそのまま伝播しCliRunnerがexit_code=1として捕捉)を変更しない。
    assert human.exit_code == 1
    assert human.exception is not None


def test_usage_error_unchanged_by_error_json() -> None:
    """Typer/Clickのusage error(必須引数欠落)はerror_jsonの対象外(§16, §32)。"""
    without_flag = runner.invoke(cli.app, ["apply-single-title-update"])
    with_flag = runner.invoke(
        cli.app, ["apply-single-title-update", "--error-json", "--manifest", "x"]
    )

    assert without_flag.exit_code != 0
    assert with_flag.exit_code != 0
    # どちらもTyperのusage errorであり、JSONは出力されない。
    with pytest.raises(json.JSONDecodeError):
        json.loads(without_flag.output)
    with pytest.raises(json.JSONDecodeError):
        json.loads(with_flag.output)

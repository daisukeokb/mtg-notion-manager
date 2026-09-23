"""CLI_ERROR_CONTRACT v1(--error-json)の対応コマンド
(import / import-article / apply-single-title-update / verify-import / doctor /
audit-duplicates / review-duplicate-conflicts / plan-title-updates)テスト。

Notion/外部サイトへは一切接続しない(すべてfake/monkeypatch)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.apply_price_link_dedupe_mutation_adapter import (
    ResultFidelityViolationError as PriceLinkResultFidelityViolationError,
)
from mtg_notion_manager.apply_price_link_dedupe_mutation_adapter import (
    build_price_link_dedupe_mutation_summary,
)
from mtg_notion_manager.config import Config, ConfigError
from mtg_notion_manager.error_contract import SCHEMA_VERSION, ErrorCategory, ErrorCode
from mtg_notion_manager.exceptions import (
    AmbiguousCardMatchError,
    IntentionalDuplicateConfigError,
    MappingError,
    MultipleDecksFoundError,
    NotionAPIError,
)
from mtg_notion_manager.import_cards_mutation_adapter import (
    ResultFidelityViolationError,
    build_import_cards_mutation_summary,
)
from mtg_notion_manager.models import (
    CardDecision,
    DeckCard,
    DeckRecord,
    ExistingDeck,
    ParsedDeckList,
)
from mtg_notion_manager.services import title_update_dry_run as planner
from mtg_notion_manager.services.apply_price_link_dedupe import ApplyLogPaths, GroupApplyOutcome
from mtg_notion_manager.services.audit_duplicates import AuditReportPaths, GroupAudit
from mtg_notion_manager.services.dedupe_cards import FailedGroupOperation, GroupWriteCompletion
from mtg_notion_manager.services.doctor import CheckResult
from mtg_notion_manager.services.import_article import ArticleImportLogPaths, ArticleImportPlan
from mtg_notion_manager.services.import_cards import (
    CardApplyResult,
    FailedWriteOperation,
    ImportCardsPlan,
    ImportCardsResult,
    PartialImportAbortedError,
    WriteCompletion,
)
from mtg_notion_manager.services.import_deck import ImportPlan
from mtg_notion_manager.services.review_duplicate_conflicts import (
    CATEGORY_PRICE_ONLY,
    DetailedGroupReview,
    ReviewReportPaths,
)
from mtg_notion_manager.services.single_card_title_update import (
    GuardedHttpCallRecord,
    SingleUpdatePreflightResult,
)
from mtg_notion_manager.services.title_update_dry_run import ConfirmedTitleUpdateEntry
from mtg_notion_manager.services.verify_import import (
    ArticleVerifyReport,
    DeckVerifyEntry,
    VerifyReportPaths,
)

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


# --- verify-import ------------------------------------------------------------


def _verify_verified_entry(deck_name: str = "デッキA") -> DeckVerifyEntry:
    return DeckVerifyEntry(
        deck_name=deck_name,
        verification_status="verified",
        verification_errors=[],
        deck_page_id="deck-1",
        deck_page_url="https://notion.so/deck-1",
        extracted_card_count=100,
        unique_card_count=87,
        existing_card_count=87,
        new_card_count=0,
        ambiguous_match_count=0,
        error_count=0,
        overrides_used=[],
        expected_relation_page_ids=["p1", "p2"],
        actual_relation_page_ids=["p1", "p2"],
        missing_relation_page_ids=[],
        unexpected_relation_page_ids=[],
    )


def _verify_mismatch_entry(deck_name: str = "デッキB") -> DeckVerifyEntry:
    return DeckVerifyEntry(
        deck_name=deck_name,
        verification_status="mismatch",
        verification_errors=["新規カードが1件あります(カードDB未登録の可能性)"],
        deck_page_id="deck-2",
        deck_page_url="https://notion.so/deck-2",
        extracted_card_count=100,
        unique_card_count=85,
        existing_card_count=84,
        new_card_count=1,
        ambiguous_match_count=0,
        error_count=0,
        overrides_used=[],
        expected_relation_page_ids=["p3"],
        actual_relation_page_ids=["p3"],
        missing_relation_page_ids=[],
        unexpected_relation_page_ids=[],
    )


def _verify_report(entries: list[DeckVerifyEntry]) -> ArticleVerifyReport:
    return ArticleVerifyReport(
        source_url=URL, all_deck_names=[e.deck_name for e in entries], entries=entries
    )


def _patch_verify_write_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "write_verify_report",
        lambda report, output_dir, timestamp=None: VerifyReportPaths(
            json_path=output_dir / "verify-import-x.json"
        ),
    )


def test_verify_import_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["verify-import", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_verify_import_verified_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_verify_write_report(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_verify_import_plan",
        lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _verify_report(
            [_verify_verified_entry()]
        ),
    )

    result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "成功数: 1" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_verify_import_verified_error_json_flag_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_verify_write_report(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_verify_import_plan",
        lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _verify_report(
            [_verify_verified_entry()]
        ),
    )

    human = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])
    structured = runner.invoke(
        cli.app, ["verify-import", URL, "--output-dir", str(tmp_path), "--error-json"]
    )

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    # 検証成功時にはJSONスキーマが存在しないため、stdout全体はJSONとして解釈できない。
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_verify_import_diff_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_verify_write_report(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_verify_import_plan",
        lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _verify_report(
            [_verify_verified_entry(), _verify_mismatch_entry()]
        ),
    )

    result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "失敗数: 1" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_verify_import_diff_error_json_flag_still_no_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """差分あり(exit 1)はexecution errorではないため、--error-jsonでもJSONを出力しない。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_verify_write_report(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_verify_import_plan",
        lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _verify_report(
            [_verify_verified_entry(), _verify_mismatch_entry()]
        ),
    )

    human = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])
    structured = runner.invoke(
        cli.app, ["verify-import", URL, "--output-dir", str(tmp_path), "--error-json"]
    )

    assert human.exit_code == structured.exit_code == 1
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_verify_import_config_error_human_and_json_exit_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    human = runner.invoke(cli.app, ["verify-import", URL])
    structured = runner.invoke(cli.app, ["verify-import", URL, "--error-json"])

    assert human.exit_code == structured.exit_code == 2
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)


def test_verify_import_config_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    result = runner.invoke(cli.app, ["verify-import", URL, "--error-json"])

    assert result.exit_code == 2
    _assert_pure_json_error(
        result.stdout,
        command="verify-import",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_verify_import_notion_api_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleVerifyReport:
        raise NotionAPIError("Notion API呼び出しに失敗しました (500): boom")

    monkeypatch.setattr(cli, "build_verify_import_plan", _raise)

    result = runner.invoke(cli.app, ["verify-import", URL, "--error-json"])

    assert result.exit_code == 2
    _assert_pure_json_error(
        result.stdout,
        command="verify-import",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


def test_verify_import_mapping_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify-importはbuild_article_import_planを内部で再利用するため、MappingError等の
    import-article由来の既存分類例外もそのまま到達し得る(§Phase B調査結果の回帰確認)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleVerifyReport:
        raise MappingError("セット名 'SPM' はマッピングできません。")

    monkeypatch.setattr(cli, "build_verify_import_plan", _raise)

    result = runner.invoke(cli.app, ["verify-import", URL, "--error-json"])

    assert result.exit_code == 2
    _assert_pure_json_error(
        result.stdout,
        command="verify-import",
        category=ErrorCategory.MAPPING,
        code=ErrorCode.UNMAPPED_VALUE,
    )


def test_verify_import_unhandled_exception_falls_back_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MtgNotionManagerErrorの階層に属さない、真に想定外の例外のケース。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> ArticleVerifyReport:
        raise ValueError("想定外の内部エラー")

    monkeypatch.setattr(cli, "build_verify_import_plan", _raise)

    structured = runner.invoke(cli.app, ["verify-import", URL, "--error-json"])
    human = runner.invoke(cli.app, ["verify-import", URL])

    _assert_pure_json_error(
        structured.stdout,
        command="verify-import",
        category=ErrorCategory.INTERNAL,
        code=ErrorCode.UNHANDLED_EXCEPTION,
    )
    # human modeでは既存動作(例外がそのまま伝播しCliRunnerがexit_code=1として捕捉)を変更しない。
    assert human.exit_code == 1
    assert human.exception is not None


# --- doctor ---------------------------------------------------------------------


def _patch_doctor_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())


def test_doctor_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["doctor", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_doctor_healthy_human_mode_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_doctor_notion(monkeypatch)
    monkeypatch.setattr(
        cli, "run_doctor", lambda config, client: [CheckResult("Notion認証", True, "OK")]
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "すべてのチェックに合格しました" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_doctor_healthy_error_json_flag_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_doctor_notion(monkeypatch)
    monkeypatch.setattr(
        cli, "run_doctor", lambda config, client: [CheckResult("Notion認証", True, "OK")]
    )

    human = runner.invoke(cli.app, ["doctor"])
    structured = runner.invoke(cli.app, ["doctor", "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    # 診断成功時にはJSONスキーマが存在しないため、stdout全体はJSONとして解釈できない。
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_doctor_finding_human_mode_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_doctor_notion(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda config, client: [CheckResult("MTG統率者DB接続", False, "スキーマ不一致")],
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "一部のチェックに失敗しました" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_doctor_finding_error_json_flag_still_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """診断チェック失敗(exit 1)はexecution errorではないため、--error-jsonでもJSONを出力しない。

    doctorのExit 1は「設定/Notion接続の実行エラー」と「診断チェック失敗」の両方に
    使われるが、この2つを混同しないことが本Work Unitで最も重要な契約である。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_doctor_notion(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda config, client: [CheckResult("MTG統率者DB接続", False, "スキーマ不一致")],
    )

    human = runner.invoke(cli.app, ["doctor"])
    structured = runner.invoke(cli.app, ["doctor", "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_doctor_config_error_human_and_json_exit_match(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    human = runner.invoke(cli.app, ["doctor"])
    structured = runner.invoke(cli.app, ["doctor", "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)


def test_doctor_config_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    result = runner.invoke(cli.app, ["doctor", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="doctor",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_doctor_notion_api_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor自体(またはNotionClient確立)がNotionAPIErrorを送出するケース。

    実運用ではrun_doctor内部の各チェックがNotionAPIErrorを個別に捕捉し
    CheckResult(診断結果)へ変換するため、この例外がcli.pyまで到達するのは
    NotionClient確立失敗など稀なケースに限られるが、doctor_command側の
    except節自体は既存コードに実在するため、そのexecution error経路を検証する。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_doctor_notion(monkeypatch)

    def _raise(config: Config, client: object) -> list[CheckResult]:
        raise NotionAPIError("Notion API呼び出しに失敗しました (500): boom")

    monkeypatch.setattr(cli, "run_doctor", _raise)

    result = runner.invoke(cli.app, ["doctor", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="doctor",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


def test_doctor_unhandled_exception_falls_back_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MtgNotionManagerErrorの階層に属さない、真に想定外の例外のケース。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_doctor_notion(monkeypatch)

    def _raise(config: Config, client: object) -> list[CheckResult]:
        raise ValueError("想定外の内部エラー")

    monkeypatch.setattr(cli, "run_doctor", _raise)

    structured = runner.invoke(cli.app, ["doctor", "--error-json"])
    human = runner.invoke(cli.app, ["doctor"])

    _assert_pure_json_error(
        structured.stdout,
        command="doctor",
        category=ErrorCategory.INTERNAL,
        code=ErrorCode.UNHANDLED_EXCEPTION,
    )
    # human modeでは既存動作(例外がそのまま伝播しCliRunnerがexit_code=1として捕捉)を変更しない。
    assert human.exit_code == 1
    assert human.exception is not None


# --- audit-duplicates -----------------------------------------------------------


def _sample_group_audit() -> GroupAudit:
    return GroupAudit(
        card_name="沼",
        pages=[{"id": "p1"}, {"id": "p2"}],
        category="auto",
        recommended_representative_id="p1",
        representative_reasons=["英語名あり"],
        conflicts=[],
        special_version_flags=[],
        price_link_differs=False,
        merged_deck_relation_count=1,
        estimated_quantity=2,
        risks=[],
        recommended_action="dedupe-cards --card-name で自動統合可能",
    )


def _patch_audit_dedupe_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())
    monkeypatch.setattr(cli, "load_intentional_duplicates", lambda: object())


def _patch_write_audit_reports(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def fake_write(
        audits: list, output_dir: Path, timestamp: str | None = None
    ) -> AuditReportPaths:
        calls.append((audits, output_dir))
        return AuditReportPaths(
            json_path=output_dir / "a.json",
            csv_path=output_dir / "a.csv",
            markdown_path=output_dir / "a.md",
        )

    monkeypatch.setattr(cli, "write_audit_reports", fake_write)
    return calls


def test_audit_duplicates_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["audit-duplicates", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_audit_duplicates_finding_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)
    write_calls = _patch_write_audit_reports(monkeypatch)
    monkeypatch.setattr(
        cli, "audit_duplicate_groups", lambda *a, **k: [_sample_group_audit()]
    )

    result = runner.invoke(cli.app, ["audit-duplicates", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert len(write_calls) == 1
    assert "自動統合可能: 1" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_audit_duplicates_finding_error_json_flag_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """監査結果(重複グループの分類分布)はexecution errorではないため、
    --error-jsonでもJSONを出力せず既存出力・レポート生成を維持する。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)
    write_calls = _patch_write_audit_reports(monkeypatch)
    monkeypatch.setattr(
        cli, "audit_duplicate_groups", lambda *a, **k: [_sample_group_audit()]
    )

    human = runner.invoke(cli.app, ["audit-duplicates", "--output-dir", str(tmp_path)])
    write_calls.clear()
    structured = runner.invoke(
        cli.app, ["audit-duplicates", "--output-dir", str(tmp_path), "--error-json"]
    )

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert len(write_calls) == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_audit_duplicates_config_error_human_and_json_exit_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    human = runner.invoke(cli.app, ["audit-duplicates"])
    structured = runner.invoke(cli.app, ["audit-duplicates", "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)


def test_audit_duplicates_config_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    result = runner.invoke(cli.app, ["audit-duplicates", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="audit-duplicates",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_audit_duplicates_missing_card_data_source_id_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_config_without_card_db() -> Config:
        return Config(
            notion_api_key="secret_test",
            commander_data_source_id="commander-ds-id",
            card_data_source_id=None,
        )

    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, ["audit-duplicates", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="audit-duplicates",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_audit_duplicates_intentional_duplicate_config_error_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新規分類(IntentionalDuplicateConfigError)がINTERNALへフォールバックしないことの回帰確認。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    def _raise() -> object:
        raise IntentionalDuplicateConfigError("intentional_duplicate_cards.jsonが不正です")

    monkeypatch.setattr(cli, "load_intentional_duplicates", _raise)

    result = runner.invoke(cli.app, ["audit-duplicates", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="audit-duplicates",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.INTENTIONAL_DUPLICATE_CONFIG_INVALID,
    )


def test_audit_duplicates_notion_api_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> list:
        raise NotionAPIError("Notion API呼び出しに失敗しました (500): boom")

    monkeypatch.setattr(cli, "audit_duplicate_groups", _raise)

    result = runner.invoke(cli.app, ["audit-duplicates", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="audit-duplicates",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


def test_audit_duplicates_unhandled_exception_falls_back_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> list:
        raise ValueError("想定外の内部エラー")

    monkeypatch.setattr(cli, "audit_duplicate_groups", _raise)

    structured = runner.invoke(cli.app, ["audit-duplicates", "--error-json"])
    human = runner.invoke(cli.app, ["audit-duplicates"])

    _assert_pure_json_error(
        structured.stdout,
        command="audit-duplicates",
        category=ErrorCategory.INTERNAL,
        code=ErrorCode.UNHANDLED_EXCEPTION,
    )
    assert human.exit_code == 1
    assert human.exception is not None


# --- review-duplicate-conflicts ---------------------------------------------------


def _sample_detailed_review() -> DetailedGroupReview:
    return DetailedGroupReview(
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


def _patch_write_review_reports(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def fake_write(
        reviews: list, output_dir: Path, timestamp: str | None = None
    ) -> ReviewReportPaths:
        calls.append((reviews, output_dir))
        return ReviewReportPaths(
            json_path=output_dir / "r.json",
            csv_path=output_dir / "r.csv",
            markdown_path=output_dir / "r.md",
        )

    monkeypatch.setattr(cli, "write_review_reports", fake_write)
    return calls


def test_review_duplicate_conflicts_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["review-duplicate-conflicts", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_review_duplicate_conflicts_finding_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)
    write_calls = _patch_write_review_reports(monkeypatch)
    monkeypatch.setattr(
        cli, "review_duplicate_conflicts", lambda *a, **k: [_sample_detailed_review()]
    )

    result = runner.invoke(cli.app, ["review-duplicate-conflicts", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert len(write_calls) == 1
    assert "対象グループ数: 1" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_review_duplicate_conflicts_finding_error_json_flag_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """要確認グループの詳細分類結果はexecution errorではないため、
    --error-jsonでもJSONを出力せず既存出力・レポート生成を維持する。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)
    write_calls = _patch_write_review_reports(monkeypatch)
    monkeypatch.setattr(
        cli, "review_duplicate_conflicts", lambda *a, **k: [_sample_detailed_review()]
    )

    human = runner.invoke(cli.app, ["review-duplicate-conflicts", "--output-dir", str(tmp_path)])
    write_calls.clear()
    structured = runner.invoke(
        cli.app, ["review-duplicate-conflicts", "--output-dir", str(tmp_path), "--error-json"]
    )

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert len(write_calls) == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_review_duplicate_conflicts_config_error_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    result = runner.invoke(cli.app, ["review-duplicate-conflicts", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="review-duplicate-conflicts",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_review_duplicate_conflicts_intentional_duplicate_config_error_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    def _raise() -> object:
        raise IntentionalDuplicateConfigError("intentional_duplicate_cards.jsonが不正です")

    monkeypatch.setattr(cli, "load_intentional_duplicates", _raise)

    result = runner.invoke(cli.app, ["review-duplicate-conflicts", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="review-duplicate-conflicts",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.INTENTIONAL_DUPLICATE_CONFIG_INVALID,
    )


def test_review_duplicate_conflicts_notion_api_error_is_pure_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> list:
        raise NotionAPIError("Notion API呼び出しに失敗しました (500): boom")

    monkeypatch.setattr(cli, "review_duplicate_conflicts", _raise)

    result = runner.invoke(cli.app, ["review-duplicate-conflicts", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="review-duplicate-conflicts",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


def test_review_duplicate_conflicts_unhandled_exception_falls_back_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_audit_dedupe_notion(monkeypatch)

    def _raise(*args: object, **kwargs: object) -> list:
        raise ValueError("想定外の内部エラー")

    monkeypatch.setattr(cli, "review_duplicate_conflicts", _raise)

    structured = runner.invoke(cli.app, ["review-duplicate-conflicts", "--error-json"])
    human = runner.invoke(cli.app, ["review-duplicate-conflicts"])

    _assert_pure_json_error(
        structured.stdout,
        command="review-duplicate-conflicts",
        category=ErrorCategory.INTERNAL,
        code=ErrorCode.UNHANDLED_EXCEPTION,
    )
    assert human.exit_code == 1
    assert human.exception is not None


def test_review_duplicate_conflicts_invalid_category_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(
        cli.app, ["review-duplicate-conflicts", "--category", "not-a-real-category"]
    )

    assert result.exit_code == 1
    assert "不明な --category" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_review_duplicate_conflicts_invalid_category_error_json_flag_still_no_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不明な--categoryはCLIの使い方の誤りでありexecution errorではないため、
    --error-jsonを指定しても既存の人間向けメッセージ・終了コード1のまま、JSONは出力しない。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    human = runner.invoke(
        cli.app, ["review-duplicate-conflicts", "--category", "not-a-real-category"]
    )
    structured = runner.invoke(
        cli.app,
        ["review-duplicate-conflicts", "--category", "not-a-real-category", "--error-json"],
    )

    assert human.exit_code == structured.exit_code == 1
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


# --- plan-title-updates -----------------------------------------------------------


def _patch_plan_title_updates_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "install_http_write_guard", lambda client: [])
    monkeypatch.setattr(cli, "ReadOnlyNotionClient", lambda client: object())


def _plan_entry(**overrides: object) -> dict:
    base = {
        "page_id": "page-1",
        "expected_current_title": "Elusive Otter",
        "confirmed_new_title": "神出鬼没のカワウソ",
        "expected_english_name": "Elusive Otter",
        "source_deck_ids": ["deck-1"],
        "verification_status": "human_confirmed",
        "verification_actor": "user",
        "verification_note": "Japanese card title explicitly confirmed by the user",
    }
    base.update(overrides)
    return base


def _write_plan_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    data = {
        "schema_version": 1,
        "purpose": "plan_existing_card_title_updates",
        "source_audit_report": None,
        "entries": entries,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _plan_entries(entries: list[dict]) -> list[planner.TitleUpdatePlanEntry]:
    return [
        planner.TitleUpdatePlanEntry(
            page_id=e["page_id"],
            current_title=e["expected_current_title"],
            expected_current_title=e["expected_current_title"],
            confirmed_new_title=e["confirmed_new_title"],
            current_english_name=e["expected_english_name"],
            expected_english_name=e["expected_english_name"],
            verification_status="human_confirmed",
            verification_actor="user",
            verification_note="note",
            current_title_matches=True,
            english_name_matches=True,
            is_archived_or_trashed=False,
            same_title_check=planner.SameTitleCheck("x", "no_existing_same_title", []),
            relation_snapshot=planner.RelationSnapshot([], 0, True, True, []),
            eligible_for_future_update=True,
            blocking_reasons=[],
        )
        for e in entries
    ]


def _eligible_dry_run_report(entries: list[dict]) -> planner.TitleUpdateDryRunReport:
    return planner.TitleUpdateDryRunReport(
        audit_timestamp="2026-07-14T00:00:00",
        manifest_path="manifest.json",
        expected_target_count=len(entries),
        entries=_plan_entries(entries),
        method_call_log=["get_page"],
        http_call_log=[],
    )


def _blocked_dry_run_report(entries: list[dict]) -> planner.TitleUpdateDryRunReport:
    report = _eligible_dry_run_report(entries)
    first = report.entries[0]
    blocked_first = planner.TitleUpdatePlanEntry(
        **{**first.__dict__, "eligible_for_future_update": False, "blocking_reasons": ["x"]}
    )
    return planner.TitleUpdateDryRunReport(
        audit_timestamp=report.audit_timestamp,
        manifest_path=report.manifest_path,
        expected_target_count=report.expected_target_count,
        entries=[blocked_first, *report.entries[1:]],
        method_call_log=report.method_call_log,
        http_call_log=[],
    )


def test_plan_title_updates_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["plan-title-updates", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_plan_title_updates_eligible_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_plan_title_updates_notion(monkeypatch)
    entries = [_plan_entry()]
    manifest_path = _write_plan_manifest(tmp_path, entries)
    monkeypatch.setattr(
        cli, "build_title_update_dry_run_plan", lambda *a, **k: _eligible_dry_run_report(entries)
    )

    result = runner.invoke(
        cli.app,
        [
            "plan-title-updates",
            "--manifest",
            str(manifest_path),
            "--expected-count",
            "1",
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 0
    assert "適用可能: 1" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_plan_title_updates_eligible_error_json_flag_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_plan_title_updates_notion(monkeypatch)
    entries = [_plan_entry()]
    manifest_path = _write_plan_manifest(tmp_path, entries)
    monkeypatch.setattr(
        cli, "build_title_update_dry_run_plan", lambda *a, **k: _eligible_dry_run_report(entries)
    )

    common_args = [
        "plan-title-updates",
        "--manifest",
        str(manifest_path),
        "--expected-count",
        "1",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    human = runner.invoke(cli.app, common_args)
    structured = runner.invoke(cli.app, [*common_args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_plan_title_updates_blocked_human_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_plan_title_updates_notion(monkeypatch)
    entries = [_plan_entry(page_id="p1"), _plan_entry(page_id="p2")]
    manifest_path = _write_plan_manifest(tmp_path, entries)
    monkeypatch.setattr(
        cli, "build_title_update_dry_run_plan", lambda *a, **k: _blocked_dry_run_report(entries)
    )

    result = runner.invoke(
        cli.app,
        [
            "plan-title-updates",
            "--manifest",
            str(manifest_path),
            "--expected-count",
            "2",
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_plan_title_updates_blocked_error_json_flag_still_no_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """1件でも適用不可(ブロック)は正常なdry-run結果でありexecution errorではないため、
    --error-jsonでもJSONを出力せず既存出力・終了コード1・レポート生成を維持する。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_plan_title_updates_notion(monkeypatch)
    entries = [_plan_entry(page_id="p1"), _plan_entry(page_id="p2")]
    manifest_path = _write_plan_manifest(tmp_path, entries)
    monkeypatch.setattr(
        cli, "build_title_update_dry_run_plan", lambda *a, **k: _blocked_dry_run_report(entries)
    )

    common_args = [
        "plan-title-updates",
        "--manifest",
        str(manifest_path),
        "--expected-count",
        "2",
        "--output-dir",
        str(tmp_path / "out"),
    ]
    human = runner.invoke(cli.app, common_args)
    structured = runner.invoke(cli.app, [*common_args, "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_plan_title_updates_config_error_human_and_json_exit_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    human = runner.invoke(
        cli.app, ["plan-title-updates", "--manifest", "x.json", "--expected-count", "1"]
    )
    structured = runner.invoke(
        cli.app,
        ["plan-title-updates", "--manifest", "x.json", "--expected-count", "1", "--error-json"],
    )

    assert human.exit_code == structured.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)


def test_plan_title_updates_config_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    result = runner.invoke(
        cli.app,
        ["plan-title-updates", "--manifest", "x.json", "--expected-count", "1", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="plan-title-updates",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_plan_title_updates_manifest_config_error_is_pure_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """件数不一致(既存のTitleUpdateManifestConfigError分類、apply-single-title-updateと共通)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    called = {"value": False}
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: called.__setitem__("value", True))
    manifest_path = _write_plan_manifest(tmp_path, [_plan_entry()])

    result = runner.invoke(
        cli.app,
        [
            "plan-title-updates",
            "--manifest",
            str(manifest_path),
            "--expected-count",
            "7",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    assert called["value"] is False  # Notionへ接続する前に失敗している
    _assert_pure_json_error(
        result.stdout,
        command="plan-title-updates",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.MANIFEST_INVALID,
    )


def test_plan_title_updates_notion_api_error_is_pure_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_plan_title_updates_notion(monkeypatch)
    manifest_path = _write_plan_manifest(tmp_path, [_plan_entry()])

    def _raise(*args: object, **kwargs: object) -> planner.TitleUpdateDryRunReport:
        raise NotionAPIError("Notion API呼び出しに失敗しました (500): boom")

    monkeypatch.setattr(cli, "build_title_update_dry_run_plan", _raise)

    result = runner.invoke(
        cli.app,
        [
            "plan-title-updates",
            "--manifest",
            str(manifest_path),
            "--expected-count",
            "1",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="plan-title-updates",
        category=ErrorCategory.PRODUCTION_API,
        code=ErrorCode.NOTION_API_ERROR,
    )


def test_plan_title_updates_unhandled_exception_falls_back_to_internal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_plan_title_updates_notion(monkeypatch)
    manifest_path = _write_plan_manifest(tmp_path, [_plan_entry()])

    def _raise(*args: object, **kwargs: object) -> planner.TitleUpdateDryRunReport:
        raise ValueError("想定外の内部エラー")

    monkeypatch.setattr(cli, "build_title_update_dry_run_plan", _raise)

    common_args = [
        "plan-title-updates",
        "--manifest",
        str(manifest_path),
        "--expected-count",
        "1",
    ]
    structured = runner.invoke(cli.app, [*common_args, "--error-json"])
    human = runner.invoke(cli.app, common_args)

    _assert_pure_json_error(
        structured.stdout,
        command="plan-title-updates",
        category=ErrorCategory.INTERNAL,
        code=ErrorCode.UNHANDLED_EXCEPTION,
    )
    assert human.exit_code == 1
    assert human.exception is not None


# --- import ----------------------------------------------------------------------

IMPORT_URL = "https://mtg-jp.com/reading/publicity/0038046/"


def _sample_import_deck_plan(
    existing: ExistingDeck | None = None, diff: list | None = None
) -> ImportPlan:
    record = DeckRecord(
        name="動き出した兵隊",
        commander="茨の吟遊詩人、べロ",
        set_name="ブルームバロウ",
        colors=["赤", "緑"],
        deck_list_url=IMPORT_URL,
    )
    return ImportPlan(record=record, existing=existing, diff=diff or [])


def _patch_execute_import_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    executed = {"count": 0}

    def _fake_execute_import(plan: object, writer: object) -> None:
        executed["count"] += 1

    monkeypatch.setattr(cli, "execute_import", _fake_execute_import)
    return executed


def test_import_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["import", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_import_confirmed_human_and_error_json_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirmation=yesで実際に書き込みが行われるケース。成功時の出力はerror_jsonの有無で
    変わらない(--error-jsonは対話確認をbypassしない)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer, deck_name=None: _sample_import_deck_plan()
    )
    executed = _patch_execute_import_counter(monkeypatch)

    human = runner.invoke(cli.app, ["import", IMPORT_URL], input="y\n")
    executed["count"] = 0
    structured = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"], input="y\n")

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert executed["count"] == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_declined_confirmation_human_and_error_json_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認拒否(decline)はexecution errorではないため、--error-jsonでもJSONを出力しない。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer, deck_name=None: _sample_import_deck_plan()
    )
    executed = _patch_execute_import_counter(monkeypatch)

    human = runner.invoke(cli.app, ["import", IMPORT_URL], input="n\n")
    structured = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"], input="n\n")

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert executed["count"] == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_eof_non_interactive_behaves_like_decline(monkeypatch: pytest.MonkeyPatch) -> None:
    """非対話環境(stdin未提供/EOF)は、このプロジェクトの実行基盤(CliRunner)上では
    click.Abortではなくdefault値(confirm拒否相当)に解決される(実測で確認済み)。
    --error-jsonはこの挙動を変更しない。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer, deck_name=None: _sample_import_deck_plan()
    )
    executed = _patch_execute_import_counter(monkeypatch)

    human = runner.invoke(cli.app, ["import", IMPORT_URL], input="")
    structured = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"], input="")

    assert human.exit_code == structured.exit_code == 0
    assert human.exception is None
    assert structured.exception is None
    assert human.stdout == structured.stdout
    assert executed["count"] == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_dry_run_human_and_error_json_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer, deck_name=None: _sample_import_deck_plan()
    )
    executed = _patch_execute_import_counter(monkeypatch)

    human = runner.invoke(cli.app, ["import", IMPORT_URL, "--dry-run"])
    structured = runner.invoke(cli.app, ["import", IMPORT_URL, "--dry-run", "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert executed["count"] == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_duplicate_skip_human_and_error_json_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    existing = ExistingDeck(page_id="p1", page_url="https://notion.so/p1", properties={})
    monkeypatch.setattr(
        cli,
        "build_import_plan",
        lambda url, writer, deck_name=None: _sample_import_deck_plan(existing=existing),
    )
    executed = _patch_execute_import_counter(monkeypatch)

    human = runner.invoke(cli.app, ["import", IMPORT_URL])
    structured = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    assert executed["count"] == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_config_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigErrorは何も印字される前に発生するため、真の意味でstdout全体が純粋なJSONになる。"""

    def _raise_config_error() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

    human = runner.invoke(cli.app, ["import", IMPORT_URL])
    structured = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)
    _assert_pure_json_error(
        structured.stdout,
        command="import",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_import_preflight_domain_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_import_plan()由来のエラーはプレビュー印字の前に発生するため、
    真の意味でstdout全体が純粋なJSONになる(書き込みは一切試行されない)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    def _raise(url: str, writer: object, deck_name: str | None = None) -> ImportPlan:
        raise MappingError("セット名 'SPM' はマッピングできません。")

    monkeypatch.setattr(cli, "build_import_plan", _raise)
    executed = _patch_execute_import_counter(monkeypatch)

    result = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"])

    assert result.exit_code == 1
    assert executed["count"] == 0
    _assert_pure_json_error(
        result.stdout,
        command="import",
        category=ErrorCategory.MAPPING,
        code=ErrorCode.UNMAPPED_VALUE,
    )


def test_import_create_page_notion_api_error_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute_import()(create_page())の失敗はconfirmationの後にしか起こり得ない。

    その時点で既にプレビューが人間向けにstdoutへ印字済みであり、対話確認自体は
    Rich console経由ではなくclick自身が行うため、この1ケースに限りstdout全体を
    純粋な1個のJSONにすることは(確認内容を人間から隠さない限り)構造上できない。
    そのためこのテストは「純粋JSON」ではなく「末尾がError Contract JSONであること」
    「書き込みが正確に1回試行されたこと」「例外メッセージが保持されること」を検証する
    (README「`import`固有の安全上の注意」を参照)。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer, deck_name=None: _sample_import_deck_plan()
    )
    call_count = {"value": 0}
    original_message = (
        "Notion APIへの接続がタイムアウトしました。この操作(POST /pages)はべき等ではなく、"
        "サーバー側で実際には処理が完了していた可能性を排除できないため自動リトライしません"
        "(手動で状態を確認してから再実行してください): boom"
    )

    def _raise(plan: object, writer: object) -> None:
        call_count["value"] += 1
        raise NotionAPIError(original_message)

    monkeypatch.setattr(cli, "execute_import", _raise)

    result = runner.invoke(cli.app, ["import", IMPORT_URL, "--error-json"], input="y\n")

    assert result.exit_code == 1
    assert call_count["value"] == 1  # 書き込みはちょうど1回だけ試行された(自動リトライなし)
    assert "プレビュー" in result.stdout  # 確認前に表示された内容は変更されない

    # 末尾がError Contract JSONであることを確認する(先頭のプレビュー分だけstdout純度が緩和される)。
    last_line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["command"] == "import"
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    # timeout特有の「サーバー側で完了していた可能性を排除できない」という安全上重要な文言が
    # 一切変更・要約されずそのまま保持されていることを確認する。
    assert payload["message"] == original_message


def test_import_human_mode_write_error_message_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """human modeでは既存のエラー表示(赤字の「エラー:」)を変更しない。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer, deck_name=None: _sample_import_deck_plan()
    )

    def _raise(plan: object, writer: object) -> None:
        raise NotionAPIError("boom")

    monkeypatch.setattr(cli, "execute_import", _raise)

    result = runner.invoke(cli.app, ["import", IMPORT_URL], input="y\n")

    assert result.exit_code == 1
    assert "エラー" in result.stdout
    assert "boom" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


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


# --- import-cards --------------------------------------------------------------

IMPORT_CARDS_URL = "https://mtg-jp.com/reading/publicity/0035593/"
IMPORT_CARDS_DECK_PAGE_ID = "39aa97c8-7142-81cb-af6e-d7a0446dea2c"


def _import_cards_card(name_ja: str) -> DeckCard:
    return DeckCard(
        name_ja=name_ja, name_en=None, quantity=1, is_commander=False, source_url=IMPORT_CARDS_URL
    )


def _import_cards_sample_plan(decisions: list[CardDecision]) -> ImportCardsPlan:
    parsed = ParsedDeckList(
        deck_name="吸血鬼の血統",
        commander_name="マウアーの太祖、ストレイファン",
        cards=[d.card for d in decisions],
        source_url=IMPORT_CARDS_URL,
    )
    return ImportCardsPlan(
        parsed=parsed, deck_page_id=IMPORT_CARDS_DECK_PAGE_ID, decisions=decisions
    )


def _patch_import_cards_build_plan(
    monkeypatch: pytest.MonkeyPatch, decisions: list[CardDecision]
) -> None:
    def _fake_build_plan(
        url: str,
        deck_page_id: str,
        repo: object,
        deck_name: str | None = None,
        allow_count_mismatch: bool = False,
        confirmed_mapping: object = None,
    ) -> ImportCardsPlan:
        return _import_cards_sample_plan(decisions)

    monkeypatch.setattr(cli, "build_import_cards_plan", _fake_build_plan)


def _assert_pure_json_v2_mutation_error(
    stdout: str, *, command: str, category: str, code: str
) -> dict:
    """v2(mutation付き)Error JSONがstdout全体で純粋な1個のJSONオブジェクトであることを確認する。"""
    assert "\x1b" not in stdout, "stdoutにANSIエスケープが含まれてはならない"
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert payload["schema_version"] == 2
    assert payload["command"] == command
    assert payload["error_category"] == category
    assert payload["error_code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    assert "mutation" in payload
    assert not (_PROHIBITED_JSON_KEYS & payload.keys())
    return payload


def test_import_cards_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["import-cards", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_import_cards_no_apply_error_json_matches_human_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--apply省略時(normal outcome)は--error-jsonの有無で出力・終了コードが変わらない。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [CardDecision(card=_import_cards_card("新カード"), action="create")]
    _patch_import_cards_build_plan(monkeypatch, decisions)

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("--apply省略時にexecute_import_cardsが呼ばれてはならない")

    monkeypatch.setattr(cli, "execute_import_cards", _fail_if_called)

    args = ["import-cards", IMPORT_CARDS_URL, "--deck-page-id", IMPORT_CARDS_DECK_PAGE_ID]
    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_cards_dry_run_error_json_matches_human_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [CardDecision(card=_import_cards_card("新カード"), action="create")]
    _patch_import_cards_build_plan(monkeypatch, decisions)

    args = [
        "import-cards",
        IMPORT_CARDS_URL,
        "--deck-page-id",
        IMPORT_CARDS_DECK_PAGE_ID,
        "--dry-run",
    ]
    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_cards_full_success_error_json_matches_human_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [CardDecision(card=_import_cards_card("新カード"), action="create")]
    _patch_import_cards_build_plan(monkeypatch, decisions)
    apply_result = ImportCardsResult(
        results=[
            CardApplyResult(card=_import_cards_card("新カード"), action="created", page_id="p1"),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    args = [
        "import-cards",
        IMPORT_CARDS_URL,
        "--deck-page-id",
        IMPORT_CARDS_DECK_PAGE_ID,
        "--apply",
    ]
    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_import_cards_error_json_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config() -> Config:
        raise ConfigError("APIキーが未設定です")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config))

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_import_cards_error_json_missing_card_data_source_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _config_without_card_db() -> Config:
        return Config(
            notion_api_key="secret_test",
            commander_data_source_id="commander-ds-id",
            card_data_source_id=None,
        )

    monkeypatch.setattr(cli.Config, "load", staticmethod(_config_without_card_db))

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_import_cards_error_json_missing_deck_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(cli.app, ["import-cards", IMPORT_CARDS_URL, "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.INPUT_VALIDATION,
        code=ErrorCode.DECK_IDENTIFIER_REQUIRED,
    )


def test_import_cards_error_json_deck_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)

    class FakeWriter:
        def __init__(self, client: object, data_source_id: str) -> None:
            pass

        def find_existing_deck(self, name: str) -> None:
            return None

    monkeypatch.setattr(cli, "NotionWriter", FakeWriter)

    result = runner.invoke(
        cli.app,
        ["import-cards", IMPORT_CARDS_URL, "--deck-name", "存在しないデッキ", "--error-json"],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.PRECONDITION,
        code=ErrorCode.DECK_NOT_FOUND,
    )


def test_import_cards_error_json_ambiguous_card_match_pre_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AmbiguousCardMatchErrorはexecute_import_cards()の書き込みループに入る前の
    gateで送出されるため、mutationを付けないv1のまま(§9/§28)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("曖昧カード"), action="ambiguous", detail="2件の候補")
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)

    def _raise(*args: object, **kwargs: object) -> None:
        raise AmbiguousCardMatchError("曖昧一致のため中止しました")

    monkeypatch.setattr(cli, "execute_import_cards", _raise)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.IDENTITY_AMBIGUITY,
        code=ErrorCode.AMBIGUOUS_CARD_MATCH,
    )
    assert "mutation" not in payload


def _known_failed_result(
    name_ja: str, *, operation: str = FailedWriteOperation.CREATE
) -> CardApplyResult:
    return CardApplyResult(
        card=_import_cards_card(name_ja),
        action="failed",
        error=f"Notion API呼び出しに失敗しました (400): {name_ja}",
        failed_operation=operation,
        failed_completion=WriteCompletion.KNOWN_FAILED,
    )


def _unknown_completion_result(
    name_ja: str, *, operation: str = FailedWriteOperation.CREATE
) -> CardApplyResult:
    return CardApplyResult(
        card=_import_cards_card(name_ja),
        action="failed",
        error=f"Notion APIへの接続がタイムアウトしました: {name_ja}",
        failed_operation=operation,
        failed_completion=WriteCompletion.UNKNOWN,
    )


def test_import_cards_error_json_known_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("カードA"), action="create"),
        CardDecision(card=_import_cards_card("カードB"), action="create"),
        CardDecision(card=_import_cards_card("カードC"), action="relation_update"),
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)
    apply_result = ImportCardsResult(
        results=[
            CardApplyResult(card=_import_cards_card("カードA"), action="created", page_id="p-a"),
            _known_failed_result("カードB"),
            CardApplyResult(
                card=_import_cards_card("カードC"), action="relation_updated", page_id="p-c"
            ),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.CARD_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "PARTIAL_MUTATION"
    assert mutation["attempted"] == 3
    assert mutation["succeeded"] == 2
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"
    assert mutation["operations"] == [{"key": "カードB", "action": "create", "state": "failed"}]


def test_import_cards_error_json_unknown_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("カードA"), action="create"),
        CardDecision(card=_import_cards_card("カードB"), action="create"),
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)
    apply_result = ImportCardsResult(
        results=[
            CardApplyResult(card=_import_cards_card("カードA"), action="created", page_id="p-a"),
            _unknown_completion_result("カードB"),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.CARD_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["attempted"] == 2
    assert mutation["succeeded"] == 1
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 1
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"
    assert mutation["operations"] == [{"key": "カードB", "action": "create", "state": "unknown"}]


def test_import_cards_error_json_unknown_relation_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("カードA"), action="relation_update"),
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)
    apply_result = ImportCardsResult(
        results=[
            _unknown_completion_result("カードA", operation=FailedWriteOperation.RELATION_UPDATE),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.CARD_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"
    assert mutation["operations"] == [
        {"key": "カードA", "action": "relation_update", "state": "unknown"}
    ]


def test_import_cards_error_json_all_known_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("カードA"), action="create"),
        CardDecision(card=_import_cards_card("カードB"), action="create"),
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)
    apply_result = ImportCardsResult(
        results=[
            _known_failed_result("カードA"),
            _known_failed_result("カードB"),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.CARD_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_FAILED"
    assert mutation["succeeded"] == 0
    assert mutation["failed"] == 2
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"


def test_import_cards_error_json_first_card_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """T6: 完了済みresultが1件もないままの中断(§26)。write loopには入っているため
    v2(mutation付き、NO_MUTATION/recovery=NONE)として扱う――pre-write gateの
    AmbiguousCardMatchError(v1)とはここで区別される。

    mutation.recovery_action=NONEは「mutation側の追加対応は不要」だけを意味し、
    top-levelのerror_category/error_code(実際の中断原因)の解消は依然として
    必要であることを、この関数はtop-levelエラーが存在することへの assert とともに
    確認する。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [CardDecision(card=_import_cards_card("未確認カード"), action="create")]
    _patch_import_cards_build_plan(monkeypatch, decisions)

    def _raise(*args: object, **kwargs: object) -> None:
        raise PartialImportAbortedError(
            "カード '未確認カード' は日本語名が未確認のため 新規作成できません(安全機構違反)。",
            completed_results=(),
        )

    monkeypatch.setattr(cli, "execute_import_cards", _raise)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.IDENTITY_AMBIGUITY,
        code=ErrorCode.UNVERIFIED_NEW_CARD,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "NO_MUTATION"
    assert mutation["attempted"] == 0
    assert mutation["recovery_action"] == "NONE"


def test_import_cards_error_json_successful_prefix_then_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功済みの書き込みがあった後の中断(§25/§36-B)。

    Work Unit本文の例は recovery=MANUAL_REVIEW_REQUIRED を期待しているが、
    MutationSummary(state=MUTATION_SUCCEEDED)へ許容されるrecovery_actionは
    NONEのみ(Phase 2Eで確定・検証済みのmutation_contract.pyの不変条件、
    tests/test_mutation_contract.py::test_no_mutation_with_reconcile...等参照)。
    「MUTATION_SUCCEEDED はattempted writesが全件成功しただけでcommand全体の
    成功を意味しない」というWork Unit自身の注記どおり、コマンド中断そのものへの
    manual review要否は、mutation.recovery_actionではなくtop-levelの
    error_category/error_code(実際の中断原因の分類)で表現する。
    この関数は意図的にrecovery=NONEを検証する(§25の文言をそのまま実装すると
    MutationSummaryのバリデーションでValueErrorになり、v2 infrastructureの
    既存の安全側の不変条件に違反するため)。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("カードA"), action="create"),
        CardDecision(card=_import_cards_card("カードB"), action="relation_update"),
        CardDecision(card=_import_cards_card("未確認カード"), action="create"),
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)

    completed = (
        CardApplyResult(card=_import_cards_card("カードA"), action="created", page_id="p-a"),
        CardApplyResult(
            card=_import_cards_card("カードB"), action="relation_updated", page_id="p-b"
        ),
    )

    def _raise(*args: object, **kwargs: object) -> None:
        raise PartialImportAbortedError(
            "カード '未確認カード' は日本語名が未確認のため 新規作成できません(安全機構違反)。",
            completed_results=completed,
        )

    monkeypatch.setattr(cli, "execute_import_cards", _raise)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.IDENTITY_AMBIGUITY,
        code=ErrorCode.UNVERIFIED_NEW_CARD,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_SUCCEEDED"
    assert mutation["attempted"] == 2
    assert mutation["succeeded"] == 2
    assert mutation["recovery_action"] == "NONE"


def test_import_cards_error_json_unknown_prefix_then_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完了済みresultの中に完了状態不明の失敗が含まれたままの中断(§36-C)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    decisions = [
        CardDecision(card=_import_cards_card("カードA"), action="create"),
        CardDecision(card=_import_cards_card("未確認カード"), action="create"),
    ]
    _patch_import_cards_build_plan(monkeypatch, decisions)

    completed = (_unknown_completion_result("カードA"),)

    def _raise(*args: object, **kwargs: object) -> None:
        raise PartialImportAbortedError(
            "カード '未確認カード' は日本語名が未確認のため 新規作成できません(安全機構違反)。",
            completed_results=completed,
        )

    monkeypatch.setattr(cli, "execute_import_cards", _raise)

    result = runner.invoke(
        cli.app,
        [
            "import-cards",
            IMPORT_CARDS_URL,
            "--deck-page-id",
            IMPORT_CARDS_DECK_PAGE_ID,
            "--apply",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="import-cards",
        category=ErrorCategory.IDENTITY_AMBIGUITY,
        code=ErrorCode.UNVERIFIED_NEW_CARD,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"


def test_import_cards_mutation_adapter_fail_closed_on_missing_metadata() -> None:
    """action=="failed"なのにfailed_operation/failed_completionが欠落した
    CardApplyResultは、誤ったmutation JSONへ変換せずfail closedする(§14/§37)。
    message文字列からの推測は行わない。
    """
    broken_result = CardApplyResult(
        card=_import_cards_card("壊れたカード"), action="failed", error="何か失敗しました"
    )

    with pytest.raises(ResultFidelityViolationError):
        build_import_cards_mutation_summary([broken_result])


# --- apply-price-link-dedupe ----------------------------------------------------


def _price_link_target_item(
    card_name: str,
    page_ids: list[str],
    category: str = "price_only",
    prices: list[float] | None = None,
) -> dict:
    return {
        "card_name": card_name,
        "review_category": category,
        "duplicate_count": len(page_ids),
        "prices": prices or [100, 200],
        "links": [],
        "merged_deck_relation_count": 0,
        "pages": [{"page_id": pid} for pid in page_ids],
    }


def _write_price_link_targets_report(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _patch_price_link_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_notion(monkeypatch)
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())


def _assert_pure_json_v2_mutation_error(
    stdout: str, *, command: str, category: str, code: str
) -> dict:
    assert "\x1b" not in stdout, "stdoutにANSIエスケープが含まれてはならない"
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert payload["schema_version"] == 2
    assert payload["command"] == command
    assert payload["error_category"] == category
    assert payload["error_code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    assert "mutation" in payload
    assert not (_PROHIBITED_JSON_KEYS & payload.keys())
    return payload


def test_apply_price_link_dedupe_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["apply-price-link-dedupe", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


def test_apply_price_link_dedupe_error_json_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise_config() -> Config:
        raise ConfigError("APIキーが未設定です")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config))
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_apply_price_link_dedupe_error_json_missing_card_data_source_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _config_without_card_db() -> Config:
        return Config(
            notion_api_key="secret_test",
            commander_data_source_id="commander-ds-id",
            card_data_source_id=None,
        )

    monkeypatch.setattr(cli.Config, "load", staticmethod(_config_without_card_db))
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_apply_price_link_dedupe_error_json_report_load_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(tmp_path / "missing.json"),
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.INPUT_VALIDATION,
        code=ErrorCode.TARGETS_REPORT_LOAD_FAILED,
    )


def test_apply_price_link_dedupe_error_json_invalid_scope_stays_human(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI usage error(不明な--scope)はError Contractの対象外(§7)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "bogus",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    assert "エラー" in result.stdout


def test_apply_price_link_dedupe_error_json_manual_scope_misuse_stays_human(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI usage error(--scope manualで代表ページID未指定)はError Contractの対象外(§7)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "manual",
            "--error-json",
        ],
    )

    assert result.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_apply_price_link_dedupe_error_json_dry_run_matches_human_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )
    outcome = GroupApplyOutcome(card_name="沼", status="planned", representative_page_id="p1")
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "canary",
        "--dry-run",
    ]

    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_apply_price_link_dedupe_error_json_success_matches_human_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )
    outcome = GroupApplyOutcome(
        card_name="沼", status="applied", representative_page_id="p1", merged_page_ids=["p2"]
    )
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    monkeypatch.setattr(
        cli,
        "write_price_link_apply_log",
        lambda outcomes, targets_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "canary",
        "--apply",
        "--output-dir",
        str(tmp_path),
    ]

    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_apply_price_link_dedupe_error_json_stale_skip_matches_human_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )
    outcome = GroupApplyOutcome(
        card_name="沼", status="skipped_stale", reason="現在の分類が変化しています"
    )
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "canary",
        "--apply",
        "--output-dir",
        str(tmp_path),
    ]

    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def _known_failed_outcome(
    card_name: str,
    *,
    operation: str = FailedGroupOperation.MARK_MERGED,
    merged: list[str] | None = None,
) -> GroupApplyOutcome:
    return GroupApplyOutcome(
        card_name=card_name,
        status="failed",
        representative_page_id="p1",
        merged_page_ids=merged or [],
        error=f"Notion API呼び出しに失敗しました: {card_name}",
        failed_operation=operation,
        failed_completion=GroupWriteCompletion.KNOWN_FAILED,
    )


def _unknown_completion_outcome(
    card_name: str,
    *,
    operation: str = FailedGroupOperation.MARK_MERGED,
    merged: list[str] | None = None,
) -> GroupApplyOutcome:
    return GroupApplyOutcome(
        card_name=card_name,
        status="failed",
        representative_page_id="p1",
        merged_page_ids=merged or [],
        error=f"Notion APIへの接続がタイムアウトしました: {card_name}",
        failed_operation=operation,
        failed_completion=GroupWriteCompletion.UNKNOWN,
    )


def test_apply_price_link_dedupe_error_json_known_partial_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T35: 代表成功+A成功後、Bのmarkが既知失敗。実行ログも引き続き書かれる。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2", "p3"])]
    )
    outcome = _known_failed_outcome("沼", merged=["A"])
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "remaining",
        "--apply",
        "--output-dir",
        str(tmp_path),
        "--error-json",
    ]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "PARTIAL_MUTATION"
    assert mutation["attempted"] == 3
    assert mutation["succeeded"] == 2
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"
    assert mutation["operations"] == [{"key": "沼", "action": "mark_merged", "state": "failed"}]
    # JSON purity要件はexecution log抑止を意味しない(§26)。
    log_files = list(tmp_path.glob("dedupe-price-apply-*.json"))
    assert len(log_files) == 1


def test_apply_price_link_dedupe_error_json_unknown_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T36: タイムアウトによる完了状態不明。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2", "p3"])]
    )
    outcome = _unknown_completion_outcome("沼", merged=["A"])
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "remaining",
        "--apply",
        "--output-dir",
        str(tmp_path),
        "--error-json",
    ]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["attempted"] == 3
    assert mutation["succeeded"] == 2
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 1
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"
    assert mutation["operations"] == [{"key": "沼", "action": "mark_merged", "state": "unknown"}]


def test_apply_price_link_dedupe_error_json_representative_known_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T37: 代表更新自体が既知失敗。mark writeは0件。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )
    outcome = _known_failed_outcome("沼", operation=FailedGroupOperation.REPRESENTATIVE_UPDATE)
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "canary",
        "--apply",
        "--output-dir",
        str(tmp_path),
        "--error-json",
    ]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_FAILED"
    assert mutation["attempted"] == 1
    assert mutation["succeeded"] == 0
    assert mutation["failed"] == 1
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"
    assert mutation["operations"] == [
        {"key": "沼", "action": "representative_update", "state": "failed"}
    ]


def test_apply_price_link_dedupe_error_json_representative_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T38: 代表更新自体がタイムアウトによる完了状態不明。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )
    outcome = _unknown_completion_outcome(
        "沼", operation=FailedGroupOperation.REPRESENTATIVE_UPDATE
    )
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "canary",
        "--apply",
        "--output-dir",
        str(tmp_path),
        "--error-json",
    ]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["attempted"] == 1
    assert mutation["unknown"] == 1
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"


def test_apply_price_link_dedupe_error_json_multiple_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T39: Group A成功(2 writes)+Group B既知部分失敗(2成功+1失敗)+Group Cスキップ(0 writes)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path,
        [
            _price_link_target_item("A", ["p1", "p2"]),
            _price_link_target_item("B", ["p3", "p4", "p5"]),
            _price_link_target_item("C", ["p6", "p7"]),
        ],
    )
    outcome_a = GroupApplyOutcome(
        card_name="A", status="applied", representative_page_id="p1", merged_page_ids=["p2"]
    )
    outcome_b = _known_failed_outcome("B", merged=["p4"])
    outcome_c = GroupApplyOutcome(card_name="C", status="skipped_stale", reason="鮮度不一致")
    monkeypatch.setattr(
        cli,
        "apply_price_link_targets",
        lambda repo, targets, apply, exclusions=None: [outcome_a, outcome_b, outcome_c],
    )
    args = [
        "apply-price-link-dedupe",
        "--targets-report",
        str(report_path),
        "--scope",
        "remaining",
        "--apply",
        "--output-dir",
        str(tmp_path),
        "--error-json",
    ]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="apply-price-link-dedupe",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["attempted"] == 5
    assert mutation["succeeded"] == 4
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0


def test_apply_price_link_dedupe_mutation_adapter_fail_closed_on_inconsistent_metadata() -> None:
    """T40: failed_operationとfailed_completionが片方だけ設定された壊れたoutcomeは
    誤ったmutation JSONへ変換せずfail closedする(§16)。message文字列からの推測は行わない。"""
    broken_outcome = GroupApplyOutcome(
        card_name="沼",
        status="failed",
        error="何か失敗しました",
        failed_operation=FailedGroupOperation.MARK_MERGED,
        failed_completion=None,
    )

    with pytest.raises(PriceLinkResultFidelityViolationError):
        build_price_link_dedupe_mutation_summary([broken_outcome])


def test_apply_price_link_dedupe_canary_hard_cap_unchanged_with_error_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§33: --error-json追加後もcanaryは最大3グループにhard-capされる
    (>3件の対象適格groupがあっても3件のみ処理される)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path,
        [
            _price_link_target_item("A", ["p1", "p2"]),
            _price_link_target_item("B", ["p3", "p4"]),
            _price_link_target_item("C", ["p5", "p6"]),
            _price_link_target_item("D", ["p7", "p8"]),
            _price_link_target_item("E", ["p9", "p10"]),
        ],
    )
    captured: dict[str, list] = {}

    def fake_apply(repo: object, targets: list, apply: bool, exclusions: object = None) -> list:
        captured["targets"] = targets
        return [
            GroupApplyOutcome(card_name=t.card_name, status="applied", representative_page_id="p1")
            for t in targets
        ]

    monkeypatch.setattr(cli, "apply_price_link_targets", fake_apply)

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "canary",
            "--apply",
            "--output-dir",
            str(tmp_path),
            "--error-json",
        ],
    )

    assert result.exit_code == 0
    assert len(captured["targets"]) == 3


def test_apply_price_link_dedupe_stale_only_error_json_stays_normal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§34: 鮮度不一致によるスキップのみの場合、--error-jsonでもError JSONを出さない。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_price_link_notion(monkeypatch)
    report_path = _write_price_link_targets_report(
        tmp_path, [_price_link_target_item("沼", ["p1", "p2"])]
    )
    outcome = GroupApplyOutcome(card_name="沼", status="skipped_stale", reason="鮮度不一致")
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "canary",
            "--apply",
            "--output-dir",
            str(tmp_path),
            "--error-json",
        ],
    )

    assert result.exit_code == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)

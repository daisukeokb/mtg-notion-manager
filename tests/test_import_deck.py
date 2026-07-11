from __future__ import annotations

import pytest

from mtg_notion_manager.exceptions import MappingError
from mtg_notion_manager.models import ExistingDeck, RawDeckData
from mtg_notion_manager.services import import_deck

SOURCE_URL = "https://magic.wizards.com/en/news/announcements/x"


class FakeFetcher:
    def __init__(self, raw: RawDeckData) -> None:
        self._raw = raw

    def matches(self, url: str) -> bool:
        return True

    def fetch(self, url: str) -> RawDeckData:
        return self._raw


class FakeWriter:
    def __init__(self, existing: ExistingDeck | None = None, diff: list | None = None) -> None:
        self.existing = existing
        self.diff = diff or []
        self.created: list = []

    def find_existing_deck(self, name: str) -> ExistingDeck | None:
        return self.existing

    def diff_against(self, existing: ExistingDeck, record) -> list:
        return self.diff

    def create_deck(self, record) -> dict:
        self.created.append(record)
        return {"id": "created"}


def _raw_deck(**overrides: object) -> RawDeckData:
    defaults = dict(
        name="動き出した兵隊",
        commander="茨の吟遊詩人、べロ",
        set_raw="BLB",
        colors_raw=["Red", "Green"],
        source_url=SOURCE_URL,
    )
    defaults.update(overrides)
    return RawDeckData(**defaults)


class TestBuildImportPlan:
    def test_normalizes_raw_data_when_no_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(import_deck, "get_fetcher", lambda url: FakeFetcher(_raw_deck()))
        writer = FakeWriter(existing=None)

        plan = import_deck.build_import_plan(SOURCE_URL, writer)

        assert plan.record.name == "動き出した兵隊"
        assert plan.record.set_name == "ブルームバロウ"
        assert plan.record.colors == ["赤", "緑"]
        assert plan.record.deck_list_url == SOURCE_URL
        assert plan.is_duplicate is False

    def test_flags_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(import_deck, "get_fetcher", lambda url: FakeFetcher(_raw_deck()))
        existing = ExistingDeck(page_id="p1", page_url="https://notion.so/p1", properties={})
        writer = FakeWriter(existing=existing, diff=[])

        plan = import_deck.build_import_plan(SOURCE_URL, writer)

        assert plan.is_duplicate is True

    def test_unknown_set_raises_mapping_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            import_deck, "get_fetcher", lambda url: FakeFetcher(_raw_deck(set_raw="ZZZ"))
        )
        writer = FakeWriter()

        with pytest.raises(MappingError):
            import_deck.build_import_plan(SOURCE_URL, writer)

    def test_unknown_color_raises_mapping_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            import_deck,
            "get_fetcher",
            lambda url: FakeFetcher(_raw_deck(colors_raw=["Red", "Pink"])),
        )
        writer = FakeWriter()

        with pytest.raises(MappingError):
            import_deck.build_import_plan(SOURCE_URL, writer)


class TestExecuteImport:
    def test_creates_when_not_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(import_deck, "get_fetcher", lambda url: FakeFetcher(_raw_deck()))
        writer = FakeWriter(existing=None)
        plan = import_deck.build_import_plan(SOURCE_URL, writer)

        import_deck.execute_import(plan, writer)

        assert len(writer.created) == 1

    def test_raises_when_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(import_deck, "get_fetcher", lambda url: FakeFetcher(_raw_deck()))
        existing = ExistingDeck(page_id="p1", page_url="https://notion.so/p1", properties={})
        writer = FakeWriter(existing=existing)
        plan = import_deck.build_import_plan(SOURCE_URL, writer)

        with pytest.raises(ValueError):
            import_deck.execute_import(plan, writer)

        assert writer.created == []

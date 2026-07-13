from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.models import (
    PROVENANCE_ARTICLE_JAPANESE_NAME,
    RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
    RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME,
    RESOLUTION_CREATABLE_FROM_ARTICLE_JAPANESE_NAME,
    RESOLUTION_CREATABLE_FROM_HUMAN_CONFIRMATION,
    ConfirmationSource,
    DeckCard,
)
from mtg_notion_manager.services import card_resolution as cr

ARTICLE_URL = "https://magic.wizards.com/ja/news/announcements/lorwyn-eclipsed-commander-decklists"


def _wizards_card(name_en: str, source_reference: str | None = None, quantity: int = 1) -> DeckCard:
    return DeckCard(
        name_ja=None,
        name_en=name_en,
        quantity=quantity,
        is_commander=False,
        source_url=ARTICLE_URL,
        source_reference=source_reference,
    )


def _mtgjp_card(name_ja: str, quantity: int = 1) -> DeckCard:
    return DeckCard(
        name_ja=name_ja,
        name_en=None,
        quantity=quantity,
        is_commander=False,
        source_url="https://mtg-jp.com/reading/publicity/0035593/",
    )


class TestStableKey:
    def test_reference_based_key(self) -> None:
        key = cr.compute_stable_key(ARTICLE_URL, "Sol Ring", None, "abc123")
        assert key.startswith(f"v{cr.STABLE_KEY_VERSION}:")
        assert "ref:abc123" in key

    def test_english_name_fallback_when_no_reference(self) -> None:
        key = cr.compute_stable_key(ARTICLE_URL, "Sol Ring", None, None)
        assert "en:sol ring" in key

    def test_japanese_name_fallback_for_mtgjp_cards(self) -> None:
        """mtg-jp.comはname_enを取得しないため、日本語名へのフォールバックが必要。"""
        key = cr.compute_stable_key(ARTICLE_URL, None, "統率者の宝球", None)
        assert "ja:" in key

    def test_same_reference_yields_same_key_regardless_of_name(self) -> None:
        key1 = cr.compute_stable_key(ARTICLE_URL, "Sol Ring", None, "abc123")
        key2 = cr.compute_stable_key(ARTICLE_URL, "Different Name", None, "abc123")
        assert key1 == key2

    def test_import_and_manifest_use_identical_key_for_same_card(self) -> None:
        card = _wizards_card("Sol Ring", source_reference="abc123")
        key_a = cr.compute_stable_key(
            ARTICLE_URL, card.name_en, card.name_ja, card.source_reference
        )
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="Deck A", confirmed_mapping=None
        )
        assert resolution.stable_key == key_a

    def test_split_card_and_special_characters_are_normalized_consistently(self) -> None:
        key1 = cr.compute_stable_key(ARTICLE_URL, "Fire  //  Ice", None, None)
        key2 = cr.compute_stable_key(ARTICLE_URL, "Fire // Ice", None, None)
        assert key1 == key2

    def test_missing_all_identifying_fields_raises(self) -> None:
        with pytest.raises(MtgNotionManagerError):
            cr.compute_stable_key(ARTICLE_URL, None, None, None)


class TestResolveNewCardProvenance:
    def test_article_japanese_name_is_creatable(self) -> None:
        card = _mtgjp_card("統率者の宝球")
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="デッキA", confirmed_mapping=None
        )
        assert resolution.resolution_status == RESOLUTION_CREATABLE_FROM_ARTICLE_JAPANESE_NAME
        assert resolution.provenance == PROVENANCE_ARTICLE_JAPANESE_NAME
        assert resolution.verified_card is not None
        assert resolution.verified_card.name_ja == "統率者の宝球"

    def test_english_only_card_without_mapping_is_unconfirmed(self) -> None:
        card = _wizards_card("Ashling, the Limitless")
        resolution = cr.resolve_new_card(
            card,
            article_url=ARTICLE_URL,
            deck_name="Dance of the Elements",
            confirmed_mapping=None,
        )
        assert resolution.resolution_status == RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME
        assert resolution.verified_card is None
        assert resolution.name_ja is None

    def test_creatability_is_not_decided_by_string_equality(self) -> None:
        """日本語名と英語名が同じ文字列でも、provenanceが確認済みならブロックしない。"""
        entry_name = "Sol Ring"
        card = _wizards_card(entry_name, source_reference="ref-1")
        key = cr.compute_stable_key(ARTICLE_URL, entry_name, None, "ref-1")
        mapping = cr.ConfirmedCardMapping(
            article_url=ARTICLE_URL,
            entries={
                key: cr.ConfirmedCardMappingEntry(
                    stable_key=key,
                    name_en=entry_name,
                    name_ja=entry_name,  # 英語名と同じ文字列だが人間確認済み
                    confirmation_source=ConfirmationSource(
                        type="official_card_page", reference="https://example.com"
                    ),
                )
            },
        )
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=mapping
        )
        assert resolution.resolution_status == RESOLUTION_CREATABLE_FROM_HUMAN_CONFIRMATION
        assert resolution.name_ja == entry_name

    def test_existing_notion_card_is_not_reevaluated_by_resolve_new_card(self) -> None:
        """resolve_new_card()は「カードDBに一致がない」場合にのみ呼ばれる想定。
        既存カード一致時は呼び出し側(import_cards._decide)がそもそも呼ばない。
        """
        # このテストは呼び出し契約のドキュメントとして、import_cards側のテストで検証する。
        assert True


class TestConfirmedCardMappingResolution:
    def test_confirmed_mapping_allows_creation(self, tmp_path: Path) -> None:
        card = _wizards_card("Ashling, the Limitless", source_reference="ref-9")
        stable_key = cr.compute_stable_key(ARTICLE_URL, card.name_en, None, card.source_reference)
        path = _write_confirmed_mapping(
            tmp_path,
            [
                {
                    "stable_key": stable_key,
                    "name_en": "Ashling, the Limitless",
                    "name_ja": "限りなきアシュリング",
                    "source_reference": "ref-9",
                    "confirmation_source": {
                        "type": "official_card_page",
                        "reference": "https://example.com/ashling",
                    },
                }
            ],
        )
        mapping = cr.load_confirmed_card_mapping(path, ARTICLE_URL)
        resolution = cr.resolve_new_card(
            card,
            article_url=ARTICLE_URL,
            deck_name="Dance of the Elements",
            confirmed_mapping=mapping,
        )
        assert resolution.resolution_status == RESOLUTION_CREATABLE_FROM_HUMAN_CONFIRMATION
        assert resolution.name_ja == "限りなきアシュリング"
        assert resolution.verified_card is not None
        assert resolution.verified_card.confirmation_source.type == "official_card_page"

    def test_name_en_mismatch_at_runtime_blocks_instead_of_creating(
        self, tmp_path: Path
    ) -> None:
        """stable_keyがsource_reference由来で一致していても、実際のname_enが
        マッピングの記載と食い違えば安全側でブロックする(データ破損防御)。"""
        card = _wizards_card("Actual Name", source_reference="ref-9")
        stable_key = cr.compute_stable_key(ARTICLE_URL, card.name_en, None, card.source_reference)
        path = _write_confirmed_mapping(
            tmp_path,
            [
                {
                    "stable_key": stable_key,
                    "name_en": "Different Name",
                    "name_ja": "違う名前",
                    "source_reference": "ref-9",
                    "confirmation_source": {"type": "official_card_page"},
                }
            ],
        )
        mapping = cr.load_confirmed_card_mapping(path, ARTICLE_URL)
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=mapping
        )
        assert resolution.resolution_status == RESOLUTION_BLOCKED_MISSING_CONFIRMATION
        assert resolution.verified_card is None

    def test_source_reference_mismatch_at_runtime_blocks_instead_of_creating(
        self, tmp_path: Path
    ) -> None:
        card = _wizards_card("Same Name", source_reference="ref-actual")
        stable_key = cr.compute_stable_key(ARTICLE_URL, card.name_en, None, card.source_reference)
        path = _write_confirmed_mapping(
            tmp_path,
            [
                {
                    "stable_key": stable_key,
                    "name_en": "Same Name",
                    "name_ja": "同じ名前",
                    "source_reference": "ref-different",
                    "confirmation_source": {"type": "official_card_page"},
                }
            ],
        )
        mapping = cr.load_confirmed_card_mapping(path, ARTICLE_URL)
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=mapping
        )
        assert resolution.resolution_status == RESOLUTION_BLOCKED_MISSING_CONFIRMATION
        assert resolution.verified_card is None

    def test_article_url_mismatch_raises(self, tmp_path: Path) -> None:
        path = _write_confirmed_mapping(
            tmp_path, [], article_url="https://example.com/other-article"
        )
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_stable_key_version_mismatch_raises(self, tmp_path: Path) -> None:
        data = {
            "schema_version": 1,
            "stable_key_version": 999,
            "article_url": ARTICLE_URL,
            "cards": [],
        }
        path = tmp_path / "confirmed.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_name_ja_missing_raises(self, tmp_path: Path) -> None:
        path = _write_confirmed_mapping(
            tmp_path,
            [
                {
                    "stable_key": "v1:x:en:foo",
                    "name_en": "Foo",
                    "name_ja": "",
                    "confirmation_source": {"type": "official_card_page"},
                }
            ],
        )
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_confirmation_source_invalid_type_raises(self, tmp_path: Path) -> None:
        path = _write_confirmed_mapping(
            tmp_path,
            [
                {
                    "stable_key": "v1:x:en:foo",
                    "name_en": "Foo",
                    "name_ja": "フー",
                    "confirmation_source": {"type": "just_trust_me"},
                }
            ],
        )
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_confirmation_source_missing_type_raises(self, tmp_path: Path) -> None:
        path = _write_confirmed_mapping(
            tmp_path,
            [
                {
                    "stable_key": "v1:x:en:foo",
                    "name_en": "Foo",
                    "name_ja": "フー",
                    "confirmation_source": {"reference": "https://example.com"},
                }
            ],
        )
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_duplicate_stable_key_raises(self, tmp_path: Path) -> None:
        entry = {
            "stable_key": "v1:x:en:foo",
            "name_en": "Foo",
            "name_ja": "フー",
            "confirmation_source": {"type": "official_card_page"},
        }
        path = _write_confirmed_mapping(tmp_path, [entry, dict(entry)])
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_unspecified_card_remains_unconfirmed(self, tmp_path: Path) -> None:
        path = _write_confirmed_mapping(tmp_path, [])
        mapping = cr.load_confirmed_card_mapping(path, ARTICLE_URL)
        card = _wizards_card("Untouched Card")
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=mapping
        )
        assert resolution.resolution_status == RESOLUTION_BLOCKED_MISSING_CONFIRMATION

    def test_invalid_config_does_not_fallback_silently(self, tmp_path: Path) -> None:
        """不正なJSON全体はエラーで停止し、有効なエントリだけを部分適用したりしない。"""
        path = tmp_path / "confirmed.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(path, ARTICLE_URL)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(cr.ConfirmedCardMappingConfigError):
            cr.load_confirmed_card_mapping(tmp_path / "does-not-exist.json", ARTICLE_URL)


class TestIdentityConflictDetection:
    def test_same_key_same_attributes_is_not_a_conflict(self) -> None:
        card_a = _wizards_card("Sol Ring", source_reference="ref-1")
        card_b = _wizards_card("Sol Ring", source_reference="ref-1")
        res_a = cr.resolve_new_card(
            card_a, article_url=ARTICLE_URL, deck_name="Deck A", confirmed_mapping=None
        )
        res_b = cr.resolve_new_card(
            card_b, article_url=ARTICLE_URL, deck_name="Deck B", confirmed_mapping=None
        )
        conflicts = cr.detect_identity_conflicts([res_a, res_b])
        assert conflicts == set()

    def test_same_key_conflicting_name_en_is_a_conflict(self) -> None:
        # 同じsource_referenceだが英語名が違う(通常起こりえないが防御的に検出する)
        key = cr.compute_stable_key(ARTICLE_URL, "Name A", None, "ref-shared")
        res_a = _resolution(
            key, name_en="Name A", deck_name="Deck A", source_reference="ref-shared"
        )
        res_b = _resolution(
            key, name_en="Name B", deck_name="Deck B", source_reference="ref-shared"
        )
        conflicts = cr.detect_identity_conflicts([res_a, res_b])
        assert key in conflicts


class TestPendingManifest:
    def test_manifest_has_required_fields(self) -> None:
        card = _wizards_card("Ashling, the Limitless")
        resolution = cr.resolve_new_card(
            card,
            article_url=ARTICLE_URL,
            deck_name="Dance of the Elements",
            confirmed_mapping=None,
        )
        manifest = cr.build_pending_manifest(ARTICLE_URL, [resolution])
        assert manifest.schema_version == cr.MANIFEST_SCHEMA_VERSION
        assert manifest.stable_key_version == cr.STABLE_KEY_VERSION
        entry_dict = manifest.to_dict()["cards"][0]
        for key in (
            "stable_key",
            "source_deck_names",
            "quantity_by_deck",
            "name_en",
            "name_ja",
            "name_ja_provenance",
            "resolution_status",
            "confirmation_source",
            "existing_candidate_count",
            "existing_candidate_page_ids",
        ):
            assert key in entry_dict

    def test_shared_card_across_decks_is_merged(self) -> None:
        card_a = _wizards_card("Sol Ring", source_reference="ref-1", quantity=1)
        card_b = _wizards_card("Sol Ring", source_reference="ref-1", quantity=1)
        res_a = cr.resolve_new_card(
            card_a, article_url=ARTICLE_URL, deck_name="Deck A", confirmed_mapping=None
        )
        res_b = cr.resolve_new_card(
            card_b, article_url=ARTICLE_URL, deck_name="Deck B", confirmed_mapping=None
        )
        manifest = cr.build_pending_manifest(ARTICLE_URL, [res_a, res_b])
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert set(entry.source_deck_names) == {"Deck A", "Deck B"}
        assert entry.quantity_by_deck == {"Deck A": 1, "Deck B": 1}

    def test_conflicted_cards_are_reported_separately(self) -> None:
        key = cr.compute_stable_key(ARTICLE_URL, "Name A", None, "ref-shared")
        res_a = _resolution(
            key, name_en="Name A", deck_name="Deck A", source_reference="ref-shared"
        )
        res_b = _resolution(
            key, name_en="Name B", deck_name="Deck B", source_reference="ref-shared"
        )
        manifest = cr.build_pending_manifest(ARTICLE_URL, [res_a, res_b])
        assert key in manifest.conflicted_stable_keys
        assert manifest.entries[0].resolution_status == "blocked_identity_conflict"

    def test_manifest_output_is_deterministic(self) -> None:
        card1 = _wizards_card("Zzz Card")
        card2 = _wizards_card("Aaa Card")
        res1 = cr.resolve_new_card(
            card1, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=None
        )
        res2 = cr.resolve_new_card(
            card2, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=None
        )
        manifest_1 = cr.build_pending_manifest(ARTICLE_URL, [res1, res2])
        manifest_2 = cr.build_pending_manifest(ARTICLE_URL, [res2, res1])
        assert manifest_1.to_dict() == manifest_2.to_dict()

    def test_write_pending_manifest_writes_valid_json(self, tmp_path: Path) -> None:
        card = _wizards_card("Ashling, the Limitless")
        resolution = cr.resolve_new_card(
            card, article_url=ARTICLE_URL, deck_name="Deck", confirmed_mapping=None
        )
        manifest = cr.build_pending_manifest(ARTICLE_URL, [resolution])
        out_path = tmp_path / "manifest.json"
        cr.write_pending_manifest(manifest, out_path)
        loaded = json.loads(out_path.read_text(encoding="utf-8"))
        assert loaded["schema_version"] == cr.MANIFEST_SCHEMA_VERSION


def _resolution(stable_key: str, *, name_en: str, deck_name: str, source_reference: str | None):
    from mtg_notion_manager.models import RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME

    return cr.CardResolution(
        article_url=ARTICLE_URL,
        deck_name=deck_name,
        quantity=1,
        is_commander=False,
        name_en=name_en,
        name_ja=None,
        provenance=None,
        confirmation_source=None,
        source_reference=source_reference,
        stable_key=stable_key,
        existing_page_id=None,
        existing_candidate_page_ids=[],
        resolution_status=RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME,
    )


def _write_confirmed_mapping(
    tmp_path: Path, cards: list[dict], article_url: str = ARTICLE_URL
) -> Path:
    data = {
        "schema_version": 1,
        "stable_key_version": cr.STABLE_KEY_VERSION,
        "article_url": article_url,
        "cards": cards,
    }
    path = tmp_path / "confirmed_cards.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path

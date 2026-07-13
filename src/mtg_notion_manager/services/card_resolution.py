"""新規カード作成の安全機構(provenance / stable key / 人間確認済みマッピング / マニフェスト)。

背景:
英語記事(magic.wizards.com)由来のカードはパース時点でname_ja(日本語名)が
取得できない。既存の import_cards._decide() は、カードDB内に一致が無ければ
無条件で action="create" を返し、CardRepository.create_card() が
card.display_name(= name_ja or name_en)をそのままNotionの日本語タイトル
プロパティへ書き込んでいた。これは英語記事のケースでは未確認の英語名が
日本語名として書き込まれることを意味する。

このモジュールは、「新規カードページは、日本語名・英語名・確認元・同定情報・
重複確認が揃った検証済み計画からのみ作成できる」という不変条件を実装する。
resolve_new_card() が唯一の判定入口であり、import_cards.py の _decide() から
呼ばれる(CardDecision.action == "create" になるのは、この関数が
creatable_from_* を返した場合のみ)。

fuzzy match・機械翻訳・類似候補の自動採用は一切行わない
(deck_page_mapping.py と同じ設計方針)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.models import (
    BLOCKED_CREATION_ACTIONS,
    PROVENANCE_ARTICLE_JAPANESE_NAME,
    PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION,
    PROVENANCE_INVALID,
    RESOLUTION_BLOCKED_IDENTITY_CONFLICT,
    RESOLUTION_BLOCKED_INVALID_MAPPING,
    RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
    RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME,
    RESOLUTION_CREATABLE_FROM_ARTICLE_JAPANESE_NAME,
    RESOLUTION_CREATABLE_FROM_HUMAN_CONFIRMATION,
    CardDecision,
    CardResolution,
    ConfirmationSource,
    DeckCard,
    VerifiedNewCard,
)
from mtg_notion_manager.parsers.card_names import normalize_card_name
from mtg_notion_manager.services.deck_page_mapping import normalize_article_url

STABLE_KEY_VERSION = 1

SUPPORTED_CONFIRMED_MAPPING_SCHEMA_VERSIONS = (1,)

_REQUIRED_CONFIRMED_ENTRY_KEYS = ("stable_key", "name_en", "name_ja", "confirmation_source")
_REQUIRED_CONFIRMATION_SOURCE_KEYS = ("type",)
_KNOWN_CONFIRMATION_SOURCE_TYPES = (
    "official_product_article",
    "official_card_page",
    "human_manual_confirmation",
)


class UnverifiedNewCardError(MtgNotionManagerError):
    """新規カード作成時、name_jaが確認済みでないままcreate_card()が呼ばれた
    (書き込み境界の防御 - 通常はresolve_new_card()が事前に止めるため到達しない想定)。
    """


class ConfirmedCardMappingConfigError(MtgNotionManagerError):
    """人間確認済みカードマッピング設定(--confirmed-card-map)が不正。"""


def compute_stable_key(
    article_url: str, name_en: str | None, name_ja: str | None, source_reference: str | None
) -> str:
    """カードの同定キーを計算する(import/verify/マニフェスト/確認済みマッピング共通)。

    優先順位:
    1. 正規化記事URL + 記事由来参照値(source_reference)
    2. 参照値がない場合、正規化記事URL + 正規化英語名(name_en)
    3. 英語名も取得できない場合(mtg-jp.com等、日本語名のみの記事)、
       正規化記事URL + 正規化日本語名(name_ja)

    3.は元の仕様書には明記されていないが、mtg-jp.comパーサーは構造上
    name_enを一切取得しないため、英語名のみへのフォールバックでは
    同一記事内の全カードが同一キーに衝突してしまう。日本語記事の
    既存フローを壊さないための最小限の拡張として追加した。
    """
    normalized_url = normalize_article_url(article_url)
    if source_reference:
        basis = f"ref:{source_reference.strip()}"
    elif name_en:
        basis = f"en:{normalize_card_name(name_en)}"
    elif name_ja:
        basis = f"ja:{normalize_card_name(name_ja)}"
    else:
        raise MtgNotionManagerError(
            "stable_keyを計算できません(name_en/name_ja/source_referenceが全て空です)。"
        )
    return f"v{STABLE_KEY_VERSION}:{normalized_url}:{basis}"


@dataclass(frozen=True)
class ConfirmedCardMappingEntry:
    stable_key: str
    name_en: str | None
    name_ja: str
    confirmation_source: ConfirmationSource
    source_reference: str | None = None


@dataclass(frozen=True)
class ConfirmedCardMapping:
    article_url: str
    entries: dict[str, ConfirmedCardMappingEntry]

    def resolve(self, stable_key: str) -> ConfirmedCardMappingEntry | None:
        return self.entries.get(stable_key)


def load_confirmed_card_mapping(path: Path, article_url: str) -> ConfirmedCardMapping:
    """人間確認済みカードマッピング設定を読み込み、検証する。

    不正な設定(必須キー欠落・空文字・confirmation_source不正等)は
    未指定扱いへフォールバックせず、即座にConfirmedCardMappingConfigErrorを送出する
    (deck_page_mapping.load_deck_page_mapping()と同じ設計方針)。
    """
    if not path.exists():
        raise ConfirmedCardMappingConfigError(f"{path} が存在しません。")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfirmedCardMappingConfigError(f"{path} が有効なJSONではありません: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfirmedCardMappingConfigError(f"{path} の内容がオブジェクトではありません。")

    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_CONFIRMED_MAPPING_SCHEMA_VERSIONS:
        raise ConfirmedCardMappingConfigError(
            f"{path} のschema_version '{schema_version}' には対応していません"
            f"(対応バージョン: {SUPPORTED_CONFIRMED_MAPPING_SCHEMA_VERSIONS})。"
        )

    stable_key_version = data.get("stable_key_version")
    if stable_key_version != STABLE_KEY_VERSION:
        raise ConfirmedCardMappingConfigError(
            f"{path} のstable_key_version '{stable_key_version}' が現在の実装"
            f"(v{STABLE_KEY_VERSION})と一致しません。"
        )

    config_article_url = data.get("article_url")
    if not isinstance(config_article_url, str) or not config_article_url:
        raise ConfirmedCardMappingConfigError(f"{path} にarticle_urlがありません。")
    if normalize_article_url(config_article_url) != normalize_article_url(article_url):
        raise ConfirmedCardMappingConfigError(
            f"{path} のarticle_url '{config_article_url}' が実行対象記事"
            f" '{article_url}' と一致しません。"
        )

    raw_cards = data.get("cards")
    if not isinstance(raw_cards, list):
        raise ConfirmedCardMappingConfigError(f"{path} の 'cards' が配列ではありません。")

    entries = [_parse_confirmed_entry(raw, path, i) for i, raw in enumerate(raw_cards)]
    _validate_no_duplicate_stable_keys(entries, path)

    return ConfirmedCardMapping(
        article_url=config_article_url,
        entries={entry.stable_key: entry for entry in entries},
    )


def _parse_confirmed_entry(raw: object, path: Path, index: int) -> ConfirmedCardMappingEntry:
    if not isinstance(raw, dict):
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}] がオブジェクトではありません。"
        )

    missing = [key for key in _REQUIRED_CONFIRMED_ENTRY_KEYS if key not in raw]
    if missing:
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}] に必須キーがありません: {missing}"
        )

    stable_key = raw["stable_key"]
    name_en = raw["name_en"]
    name_ja = raw["name_ja"]
    source_reference = raw.get("source_reference")

    if not isinstance(stable_key, str) or not stable_key:
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].stable_key が空、または文字列ではありません。"
        )
    if name_en is not None and (not isinstance(name_en, str) or not name_en):
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].name_en は非空文字列またはnullである必要があります。"
        )
    if not isinstance(name_ja, str) or not name_ja:
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].name_ja が空、または文字列ではありません。"
        )
    if source_reference is not None and (
        not isinstance(source_reference, str) or not source_reference
    ):
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].source_reference は"
            "非空文字列またはnullである必要があります。"
        )

    confirmation_source = _parse_confirmation_source(raw.get("confirmation_source"), path, index)

    return ConfirmedCardMappingEntry(
        stable_key=stable_key,
        name_en=name_en,
        name_ja=name_ja,
        confirmation_source=confirmation_source,
        source_reference=source_reference,
    )


def _parse_confirmation_source(raw: object, path: Path, index: int) -> ConfirmationSource:
    if not isinstance(raw, dict):
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].confirmation_source がオブジェクトではありません。"
        )
    missing = [key for key in _REQUIRED_CONFIRMATION_SOURCE_KEYS if key not in raw]
    if missing:
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].confirmation_source に必須キーがありません: {missing}"
        )
    source_type = raw["type"]
    if not isinstance(source_type, str) or not source_type:
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].confirmation_source.type が空、または文字列ではありません。"
        )
    if source_type not in _KNOWN_CONFIRMATION_SOURCE_TYPES:
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].confirmation_source.type '{source_type}' が未知の種別です"
            f"(既知の種別: {_KNOWN_CONFIRMATION_SOURCE_TYPES})。"
        )
    reference = raw.get("reference")
    if reference is not None and not isinstance(reference, str):
        raise ConfirmedCardMappingConfigError(
            f"{path} の cards[{index}].confirmation_source.reference は"
            "文字列またはnullである必要があります。"
        )
    return ConfirmationSource(type=source_type, reference=reference)


def _validate_no_duplicate_stable_keys(
    entries: list[ConfirmedCardMappingEntry], path: Path
) -> None:
    seen: set[str] = set()
    for entry in entries:
        if entry.stable_key in seen:
            raise ConfirmedCardMappingConfigError(
                f"{path}: stable_key '{entry.stable_key}' が重複しています。"
            )
        seen.add(entry.stable_key)


# --- 新規カード判定(resolve_new_card) ---------------------------------------


def resolve_new_card(
    card: DeckCard,
    *,
    article_url: str,
    deck_name: str,
    confirmed_mapping: ConfirmedCardMapping | None,
) -> CardResolution:
    """カードDB内に一致が見つからなかったカード1件について、新規作成の可否を判定する。

    文字列比較ではなくprovenanceで判定する。日本語名と英語名が同じ文字列でも、
    provenanceが確認済み(article_japanese_name/explicit_human_confirmation)なら
    文字列一致だけを理由に拒否しない。
    """
    stable_key = compute_stable_key(article_url, card.name_en, card.name_ja, card.source_reference)

    if card.name_ja:
        # 記事から直接取得した日本語名がある(mtg-jp.com等)。
        # このサイトのパーサーは常にname_en=Noneのため、英語名との整合性確認は
        # 「英語名が取得できている場合に限り」行う(既存の日本語記事フローを壊さないため)。
        return CardResolution(
            article_url=article_url,
            deck_name=deck_name,
            quantity=card.quantity,
            is_commander=card.is_commander,
            name_en=card.name_en,
            name_ja=card.name_ja,
            provenance=PROVENANCE_ARTICLE_JAPANESE_NAME,
            confirmation_source=None,
            source_reference=card.source_reference,
            stable_key=stable_key,
            existing_page_id=None,
            existing_candidate_page_ids=[],
            resolution_status=RESOLUTION_CREATABLE_FROM_ARTICLE_JAPANESE_NAME,
            verified_card=VerifiedNewCard(
                name_ja=card.name_ja,
                name_en=card.name_en,
                provenance=PROVENANCE_ARTICLE_JAPANESE_NAME,
                confirmation_source=None,
                source_url=article_url,
                source_reference=card.source_reference,
                stable_key=stable_key,
                quantity=card.quantity,
                is_commander=card.is_commander,
            ),
        )

    # 記事から日本語名が取得できない(英語記事由来)。人間確認済みマッピングでのみ
    # 新規作成を許可する。
    if confirmed_mapping is None:
        return CardResolution(
            article_url=article_url,
            deck_name=deck_name,
            quantity=card.quantity,
            is_commander=card.is_commander,
            name_en=card.name_en,
            name_ja=None,
            provenance=None,
            confirmation_source=None,
            source_reference=card.source_reference,
            stable_key=stable_key,
            existing_page_id=None,
            existing_candidate_page_ids=[],
            resolution_status=RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME,
            block_reason=(
                "記事から日本語名を取得できず、--confirmed-card-map も指定されていません"
                "(人間による確認が必要です)。"
            ),
        )

    entry = confirmed_mapping.resolve(stable_key)
    if entry is None:
        return CardResolution(
            article_url=article_url,
            deck_name=deck_name,
            quantity=card.quantity,
            is_commander=card.is_commander,
            name_en=card.name_en,
            name_ja=None,
            provenance=None,
            confirmation_source=None,
            source_reference=card.source_reference,
            stable_key=stable_key,
            existing_page_id=None,
            existing_candidate_page_ids=[],
            resolution_status=RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
            block_reason=(
                f"--confirmed-card-map にこのカード(stable_key: {stable_key})の"
                " 確認済みエントリがありません。"
            ),
        )

    # 防御的な再検証: ロード時に検証済みだが、万一異なるカードへ誤って
    # 適用されていないか、実際のカード属性と突き合わせて再確認する。
    if entry.name_en != card.name_en:
        return CardResolution(
            article_url=article_url,
            deck_name=deck_name,
            quantity=card.quantity,
            is_commander=card.is_commander,
            name_en=card.name_en,
            name_ja=None,
            provenance=PROVENANCE_INVALID,
            confirmation_source=entry.confirmation_source,
            source_reference=card.source_reference,
            stable_key=stable_key,
            existing_page_id=None,
            existing_candidate_page_ids=[],
            resolution_status=RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
            block_reason=(
                f"確認済みマッピングのname_en '{entry.name_en}' が実際のカード"
                f" '{card.name_en}' と一致しません(stable_key: {stable_key})。"
            ),
        )

    if entry.source_reference is not None and entry.source_reference != card.source_reference:
        return CardResolution(
            article_url=article_url,
            deck_name=deck_name,
            quantity=card.quantity,
            is_commander=card.is_commander,
            name_en=card.name_en,
            name_ja=None,
            provenance=PROVENANCE_INVALID,
            confirmation_source=entry.confirmation_source,
            source_reference=card.source_reference,
            stable_key=stable_key,
            existing_page_id=None,
            existing_candidate_page_ids=[],
            resolution_status=RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
            block_reason=(
                f"確認済みマッピングのsource_reference '{entry.source_reference}' が"
                f" 実際のカード '{card.source_reference}' と一致しません"
                f"(stable_key: {stable_key})。"
            ),
        )

    return CardResolution(
        article_url=article_url,
        deck_name=deck_name,
        quantity=card.quantity,
        is_commander=card.is_commander,
        name_en=card.name_en,
        name_ja=entry.name_ja,
        provenance=PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION,
        confirmation_source=entry.confirmation_source,
        source_reference=card.source_reference,
        stable_key=stable_key,
        existing_page_id=None,
        existing_candidate_page_ids=[],
        resolution_status=RESOLUTION_CREATABLE_FROM_HUMAN_CONFIRMATION,
        verified_card=VerifiedNewCard(
            name_ja=entry.name_ja,
            name_en=card.name_en,
            provenance=PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION,
            confirmation_source=entry.confirmation_source,
            source_url=article_url,
            source_reference=card.source_reference,
            stable_key=stable_key,
            quantity=card.quantity,
            is_commander=card.is_commander,
        ),
    )


# --- identity conflict検出(複数デッキ横断) -----------------------------------


def detect_identity_conflicts(resolutions: list[CardResolution]) -> set[str]:
    """同一stable_keyで属性(英語名・参照値)が矛盾するカードのstable_keyを返す。

    複数デッキに同じカードが出現すること自体は正常(quantity_by_deckで束ねる)。
    矛盾とみなすのは、同じstable_keyなのにname_en/source_referenceが食い違う場合のみ。
    """
    by_key: dict[str, list[CardResolution]] = {}
    for r in resolutions:
        by_key.setdefault(r.stable_key, []).append(r)

    conflicted: set[str] = set()
    for key, group in by_key.items():
        name_en_values = {g.name_en for g in group if g.name_en}
        ref_values = {g.source_reference for g in group if g.source_reference}
        if len(name_en_values) > 1 or len(ref_values) > 1:
            conflicted.add(key)
    return conflicted


# --- 確認待ちマニフェスト -----------------------------------------------------

MANIFEST_SCHEMA_VERSION = 1

_SLUG_INVALID_RE = re.compile(r"[^0-9a-zA-Z]+")


@dataclass(frozen=True)
class PendingManifestEntry:
    stable_key: str
    source_deck_names: list[str]
    quantity_by_deck: dict[str, int]
    name_en: str | None
    name_ja: str | None
    name_ja_provenance: str | None
    source_reference: str | None
    is_commander: bool
    resolution_status: str
    confirmation_source: dict | None
    existing_candidate_count: int
    existing_candidate_page_ids: list[str]


@dataclass(frozen=True)
class PendingCardManifest:
    schema_version: int
    stable_key_version: int
    article_url: str
    entries: list[PendingManifestEntry]
    conflicted_stable_keys: list[str]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "stable_key_version": self.stable_key_version,
            "article_url": self.article_url,
            "cards": [_manifest_entry_to_dict(e) for e in self.entries],
            "identity_conflicts": self.conflicted_stable_keys,
        }


def _manifest_entry_to_dict(entry: PendingManifestEntry) -> dict:
    return {
        "stable_key": entry.stable_key,
        "source_deck_names": entry.source_deck_names,
        "quantity_by_deck": entry.quantity_by_deck,
        "name_en": entry.name_en,
        "name_ja": entry.name_ja,
        "name_ja_provenance": entry.name_ja_provenance,
        "source_reference": entry.source_reference,
        "is_commander": entry.is_commander,
        "resolution_status": entry.resolution_status,
        "confirmation_source": entry.confirmation_source,
        "existing_candidate_count": entry.existing_candidate_count,
        "existing_candidate_page_ids": entry.existing_candidate_page_ids,
    }


def build_pending_manifest(
    article_url: str, resolutions: list[CardResolution]
) -> PendingCardManifest:
    """未確認(および確認済み)カードのマニフェストを、stable_key単位で決定的に構築する。

    同一stable_keyは1件へ統合し、採用デッキ名(重複なし・出現順)と
    デッキ別数量を保持する。identity conflictがあるstable_keyは通常カードとして
    統合せず、conflicted_stable_keysへ別出力する(entriesには resolution_status を
    blocked_identity_conflict として1件も含めない、という意味ではなく、
    双方の情報を保持したまま conflict である旨を明示するため、対象カードは
    entries内にresolution_status上書き済みの形で残す)。
    出力順序はstable_keyの昇順で固定する(同じ入力から同じ出力になるようにするため)。
    """
    conflicted = detect_identity_conflicts(resolutions)

    grouped: dict[str, list[CardResolution]] = {}
    for r in resolutions:
        grouped.setdefault(r.stable_key, []).append(r)

    entries: list[PendingManifestEntry] = []
    for stable_key in sorted(grouped):
        group = grouped[stable_key]
        first = group[0]
        source_deck_names: list[str] = []
        quantity_by_deck: dict[str, int] = {}
        for r in group:
            if r.deck_name not in quantity_by_deck:
                source_deck_names.append(r.deck_name)
            quantity_by_deck[r.deck_name] = quantity_by_deck.get(r.deck_name, 0) + r.quantity

        resolution_status = (
            RESOLUTION_BLOCKED_IDENTITY_CONFLICT
            if stable_key in conflicted
            else first.resolution_status
        )

        entries.append(
            PendingManifestEntry(
                stable_key=stable_key,
                source_deck_names=source_deck_names,
                quantity_by_deck=quantity_by_deck,
                name_en=first.name_en,
                name_ja=first.name_ja,
                name_ja_provenance=first.provenance,
                source_reference=first.source_reference,
                is_commander=any(r.is_commander for r in group),
                resolution_status=resolution_status,
                confirmation_source=(
                    first.confirmation_source.to_dict() if first.confirmation_source else None
                ),
                existing_candidate_count=len(first.existing_candidate_page_ids),
                existing_candidate_page_ids=first.existing_candidate_page_ids,
            )
        )

    return PendingCardManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        stable_key_version=STABLE_KEY_VERSION,
        article_url=article_url,
        entries=entries,
        conflicted_stable_keys=sorted(conflicted),
    )


# --- dry-runサマリー(import-article/import-cards/verify-import共通) -----------


@dataclass(frozen=True)
class ResolutionSummary:
    """新規カードのprovenance別内訳(dry-runサマリー表示用)。"""

    existing_count: int
    creatable_from_article_japanese_name_count: int
    creatable_from_human_confirmation_count: int
    pending_confirmation_count: int
    identity_conflict_count: int
    ambiguous_count: int
    config_error_count: int

    @property
    def new_card_count(self) -> int:
        """Notionに未登録のカード数(作成可能・確認待ちの両方を含む)。"""
        return (
            self.creatable_from_article_japanese_name_count
            + self.creatable_from_human_confirmation_count
            + self.pending_confirmation_count
            + self.identity_conflict_count
            + self.config_error_count
        )

    @property
    def is_fully_applicable(self) -> bool:
        """全カードが既存一致または作成可能(=preflight成功)かどうか。"""
        return (
            self.pending_confirmation_count == 0
            and self.identity_conflict_count == 0
            and self.ambiguous_count == 0
            and self.config_error_count == 0
        )


def summarize_decisions(decisions: list[CardDecision]) -> ResolutionSummary:
    existing = 0
    from_article = 0
    from_human = 0
    pending = 0
    conflict = 0
    ambiguous = 0
    config_error = 0

    for d in decisions:
        if d.action in ("unchanged", "relation_update"):
            existing += 1
        elif d.action == "ambiguous":
            ambiguous += 1
        elif d.action == "create":
            provenance = d.resolution.provenance if d.resolution else None
            if provenance == PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION:
                from_human += 1
            else:
                from_article += 1
        elif d.action == RESOLUTION_BLOCKED_IDENTITY_CONFLICT:
            conflict += 1
        elif d.action == RESOLUTION_BLOCKED_INVALID_MAPPING:
            config_error += 1
        elif d.action in BLOCKED_CREATION_ACTIONS:
            pending += 1
        # "error" などその他のactionはこのサマリーでは扱わない(呼び出し側のerror_countで扱う)。

    return ResolutionSummary(
        existing_count=existing,
        creatable_from_article_japanese_name_count=from_article,
        creatable_from_human_confirmation_count=from_human,
        pending_confirmation_count=pending,
        identity_conflict_count=conflict,
        ambiguous_count=ambiguous,
        config_error_count=config_error,
    )


def write_pending_manifest(manifest: PendingCardManifest, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return path

"""意図的に保持する重複カードグループの設定。

このファイルは audit-duplicates の「表示分類」を変えるだけであり、以下には
一切影響しない:
- import時のカード照合(card_repository.py / card_match_overrides.json)
- カード新規登録・リレーション追加
- 重複統合(dedupe-cards)・代表ページ選定
- 統合済み(=true)プロパティの更新
- ページ削除・ゴミ箱移動

正規化済みの完全一致(ページID集合の完全一致 + カード名の一致)にのみ適用する。
部分一致や推測による自動判定は行わない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.exceptions import IntentionalDuplicateConfigError
from mtg_notion_manager.parsers.card_names import normalize_card_name

DEFAULT_INTENTIONAL_DUPLICATES_PATH = Path("config/intentional_duplicate_cards.json")

_REQUIRED_KEYS = ("card_name_en", "card_name_ja", "page_ids", "reason", "enabled")


@dataclass(frozen=True)
class IntentionalDuplicateGroup:
    card_name_en: str
    card_name_ja: str
    page_ids: frozenset[str]
    reason: str
    enabled: bool


@dataclass(frozen=True)
class IntentionalDuplicateConfig:
    groups: list[IntentionalDuplicateGroup]

    def find_matching_group(
        self, page_ids: frozenset[str], name_ja: str | None, name_en: str | None
    ) -> IntentionalDuplicateGroup | None:
        """有効かつページID集合が完全一致し、カード名も一致するグループを返す。

        ページID集合は順不同で比較する(frozenset同士の比較のため)。部分一致は不可。
        """
        for group in self.groups:
            if not group.enabled:
                continue
            if group.page_ids != page_ids:
                continue
            name_matches = (
                name_ja is not None
                and normalize_card_name(name_ja) == normalize_card_name(group.card_name_ja)
            ) or (
                name_en is not None
                and normalize_card_name(name_en) == normalize_card_name(group.card_name_en)
            )
            if not name_matches:
                continue
            return group
        return None


def load_intentional_duplicates(
    path: Path = DEFAULT_INTENTIONAL_DUPLICATES_PATH,
) -> IntentionalDuplicateConfig:
    if not path.exists():
        return IntentionalDuplicateConfig(groups=[])

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntentionalDuplicateConfigError(f"{path} が有効なJSONではありません: {exc}") from exc

    if not isinstance(data, dict) or "groups" not in data:
        raise IntentionalDuplicateConfigError(f"{path} に 'groups' キーがありません。")

    raw_groups = data["groups"]
    if not isinstance(raw_groups, list):
        raise IntentionalDuplicateConfigError(f"{path} の 'groups' が配列ではありません。")

    groups = [_parse_group(raw, path, i) for i, raw in enumerate(raw_groups)]
    _validate_no_cross_group_page_id_overlap(groups, path)
    _validate_no_conflicting_card_names(groups, path)
    return IntentionalDuplicateConfig(groups=groups)


def _parse_group(raw: object, path: Path, index: int) -> IntentionalDuplicateGroup:
    if not isinstance(raw, dict):
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}] がオブジェクトではありません。"
        )

    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}] に必須キーがありません: {missing}"
        )

    card_name_en = raw["card_name_en"]
    card_name_ja = raw["card_name_ja"]
    page_ids_raw = raw["page_ids"]
    reason = raw["reason"]
    enabled = raw["enabled"]

    if not isinstance(card_name_en, str) or not card_name_en:
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].card_name_en が空、または文字列ではありません。"
        )
    if not isinstance(card_name_ja, str) or not card_name_ja:
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].card_name_ja が空、または文字列ではありません。"
        )
    if not isinstance(page_ids_raw, list):
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].page_ids が配列ではありません。"
        )
    if len(page_ids_raw) < 2:
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].page_ids は2件以上指定してください"
            f"(実際: {len(page_ids_raw)}件)。"
        )
    if any(not isinstance(pid, str) or not pid for pid in page_ids_raw):
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].page_ids に空文字または文字列以外の値があります。"
        )
    if len(set(page_ids_raw)) != len(page_ids_raw):
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].page_ids 内に重複したpage_idがあります。"
        )
    if not isinstance(reason, str) or not reason:
        raise IntentionalDuplicateConfigError(f"{path} の groups[{index}].reason が空です。")
    if not isinstance(enabled, bool):
        raise IntentionalDuplicateConfigError(
            f"{path} の groups[{index}].enabled はboolean(true/false)である必要があります。"
        )

    return IntentionalDuplicateGroup(
        card_name_en=card_name_en,
        card_name_ja=card_name_ja,
        page_ids=frozenset(page_ids_raw),
        reason=reason,
        enabled=enabled,
    )


def _validate_no_cross_group_page_id_overlap(
    groups: list[IntentionalDuplicateGroup], path: Path
) -> None:
    seen: dict[str, int] = {}
    for i, group in enumerate(groups):
        for page_id in group.page_ids:
            if page_id in seen:
                raise IntentionalDuplicateConfigError(
                    f"{path}: page_id '{page_id}' が groups[{seen[page_id]}] と"
                    f" groups[{i}] の両方に含まれています(矛盾)。"
                )
            seen[page_id] = i


def _validate_no_conflicting_card_names(
    groups: list[IntentionalDuplicateGroup], path: Path
) -> None:
    seen: dict[str, int] = {}
    for i, group in enumerate(groups):
        for key in (
            normalize_card_name(group.card_name_ja),
            normalize_card_name(group.card_name_en),
        ):
            if key in seen and seen[key] != i:
                raise IntentionalDuplicateConfigError(
                    f"{path}: カード名 '{key}' が groups[{seen[key]}] と"
                    f" groups[{i}] の両方で設定されています(矛盾する複数設定)。"
                )
            seen[key] = i

"""カード照合の曖昧一致を明示的に解消するためのオーバーライド設定。

CardRepository.find_match() は原則として英語名→日本語名の完全一致で1件に
絞り込むが、意図的に複数レコード(特殊仕様違いなど)を残したいカードでは
複数候補が残り曖昧一致になる。このファイルの設定は、そうしたケースで
「採用デッキのリレーション先として使う代表ページ」を明示的に指定するためのもの。

正規化済み完全一致のカード名にのみ適用する(fuzzy matchや自動選択は行わない)。
指定した page_id が実際の曖昧一致候補に含まれない場合は、他のページへ
フォールバックせず CardMatchOverrideError を送出して停止する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.exceptions import CardMatchOverrideError
from mtg_notion_manager.parsers.card_names import normalize_card_name

DEFAULT_OVERRIDES_PATH = Path("config/card_match_overrides.json")


@dataclass(frozen=True)
class OverrideEntry:
    canonical_page_id: str
    reason: str


@dataclass(frozen=True)
class CardMatchOverrides:
    by_japanese_name: dict[str, OverrideEntry]
    by_english_name: dict[str, OverrideEntry]

    def resolve(self, name_ja: str | None, name_en: str | None) -> OverrideEntry | None:
        """英語名を優先し、正規化済み完全一致でオーバーライドを探す。"""
        if name_en:
            entry = self.by_english_name.get(normalize_card_name(name_en))
            if entry is not None:
                return entry
        if name_ja:
            entry = self.by_japanese_name.get(normalize_card_name(name_ja))
            if entry is not None:
                return entry
        return None


def load_card_match_overrides(path: Path = DEFAULT_OVERRIDES_PATH) -> CardMatchOverrides:
    if not path.exists():
        return CardMatchOverrides(by_japanese_name={}, by_english_name={})

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CardMatchOverrideError(f"{path} が有効なJSONではありません: {exc}") from exc

    if not isinstance(data, dict):
        raise CardMatchOverrideError(f"{path} の内容がオブジェクトではありません。")

    by_ja = _parse_section(data.get("by_japanese_name", {}), path, "by_japanese_name")
    by_en = _parse_section(data.get("by_english_name", {}), path, "by_english_name")
    return CardMatchOverrides(by_japanese_name=by_ja, by_english_name=by_en)


def _parse_section(section: object, path: Path, section_name: str) -> dict[str, OverrideEntry]:
    if not isinstance(section, dict):
        raise CardMatchOverrideError(f"{path} の '{section_name}' がオブジェクトではありません。")

    result: dict[str, OverrideEntry] = {}
    for name, entry in section.items():
        if not isinstance(entry, dict) or "canonical_page_id" not in entry:
            raise CardMatchOverrideError(
                f"{path} の '{section_name}.{name}' に canonical_page_id がありません。"
            )
        canonical_page_id = entry["canonical_page_id"]
        if not isinstance(canonical_page_id, str) or not canonical_page_id:
            raise CardMatchOverrideError(
                f"{path} の '{section_name}.{name}.canonical_page_id' が不正です。"
            )
        result[normalize_card_name(name)] = OverrideEntry(
            canonical_page_id=canonical_page_id, reason=str(entry.get("reason", ""))
        )
    return result

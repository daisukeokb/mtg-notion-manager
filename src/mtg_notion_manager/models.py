from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RawDeckData:
    """フェッチャーがページから抽出した、マッピング前の生データ。"""

    name: str
    commander: str
    set_raw: str
    colors_raw: list[str]
    source_url: str


@dataclass(frozen=True)
class DeckRecord:
    """Notionへ書き込む、正規化済みのデッキレコード。"""

    name: str
    commander: str
    set_name: str
    colors: list[str]
    deck_list_url: str
    owned_status: str = "所有"
    deck_type: str = "構築済み"
    modification_status: str = "未改造"

    def to_notion_properties(self) -> dict:
        return {
            "名前": {"title": [{"text": {"content": self.name}}]},
            "統率者": {"rich_text": [{"text": {"content": self.commander}}]},
            "発売セット": {"select": {"name": self.set_name}},
            "色": {"multi_select": [{"name": c} for c in self.colors]},
            "デッキリスト": {"url": self.deck_list_url},
            "所有状況": {"select": {"name": self.owned_status}},
            "タイプ": {"select": {"name": self.deck_type}},
            "改造状況": {"select": {"name": self.modification_status}},
        }

    def to_preview_dict(self) -> dict:
        return {
            "名前": self.name,
            "統率者": self.commander,
            "発売セット": self.set_name,
            "色": self.colors,
            "デッキリスト": self.deck_list_url,
            "所有状況": self.owned_status,
            "タイプ": self.deck_type,
            "改造状況": self.modification_status,
        }


@dataclass(frozen=True)
class ExistingDeck:
    """Notion上に既に存在する同名デッキ(重複チェック結果)。"""

    page_id: str
    page_url: str
    properties: dict

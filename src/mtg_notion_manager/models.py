from __future__ import annotations

from dataclasses import dataclass

from mtg_notion_manager.exceptions import DeckCardValidationError


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


@dataclass(frozen=True)
class DeckCard:
    """デッキリストから抽出した1種類のカード(マッピング前)。

    同名カードが複数箇所に現れる場合は呼び出し側で quantity を合算してから
    生成すること(このクラス自体は1エントリを表す)。
    """

    name_ja: str | None
    name_en: str | None
    quantity: int
    is_commander: bool
    source_url: str

    def __post_init__(self) -> None:
        if not self.name_ja and not self.name_en:
            raise DeckCardValidationError(f"カード名が空です(source: {self.source_url})")
        if self.quantity < 1:
            raise DeckCardValidationError(
                f"枚数が不正です(quantity={self.quantity}, card={self.display_name})"
            )

    @property
    def display_name(self) -> str:
        return self.name_ja or self.name_en or "?"


@dataclass(frozen=True)
class ParsedDeckList:
    """1デッキ分のカードリスト抽出結果(マッピング前)。"""

    deck_name: str
    commander_name: str
    cards: list[DeckCard]
    source_url: str

    @property
    def total_quantity(self) -> int:
        return sum(card.quantity for card in self.cards)


@dataclass(frozen=True)
class ExistingCard:
    """Notion上に既に存在するカード。"""

    page_id: str
    page_url: str
    properties: dict


@dataclass(frozen=True)
class CardDecision:
    """1カードに対するNotion登録計画(dry-run/適用で共有する)。

    action は以下のいずれか:
    - "create": カードDBに新規ページを作成する
    - "relation_update": 既存カードに採用デッキのリレーションを追加する(所持もtrue化しうる)
    - "unchanged": 既存カードで変更不要(既にリレーション済み・所持済み)
    - "ambiguous": カードDB内で候補が複数あり一意に決定できない
    - "error": その他の理由で処理できない
    """

    card: DeckCard
    action: str
    existing: ExistingCard | None = None
    detail: str = ""
    owned_will_change: bool = False
    override_used: str | None = None

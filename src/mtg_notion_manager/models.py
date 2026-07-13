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
    #: 記事側の識別子的な生値(例: wizards.comデッキリスト行末の[...])。
    #: 意味を断定せず(Gatherer ID等と決め打ちしない)、欠落を許容する。
    #: stable_key計算・新規カード確認済みマッピングの照合にのみ使う。
    source_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.name_ja and not self.name_en:
            raise DeckCardValidationError(f"カード名が空です(source: {self.source_url})")
        if self.quantity < 1:
            raise DeckCardValidationError(
                f"枚数が不正です(quantity={self.quantity}, card={self.display_name})"
            )

    @property
    def display_name(self) -> str:
        """表示用の名前。新規カード作成の安全判定には使用しないこと
        (name_jaが未確認でもname_enへフォールバックしてしまうため)。
        """
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


# --- 新規カード作成の安全性検証(provenance/stable key/書き込み境界) -------------
#
# 英語記事(magic.wizards.com)由来のカードはパース時点でname_jaが取得できない。
# 「新規カードページは、日本語名・英語名・確認元・同定情報・重複確認が揃った
# 検証済み計画からのみ作成できる」という不変条件を型で表現するための一群。

PROVENANCE_EXISTING_NOTION_CARD = "existing_notion_card"
PROVENANCE_ARTICLE_JAPANESE_NAME = "article_japanese_name"
PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION = "explicit_human_confirmation"
PROVENANCE_UNCONFIRMED = "unconfirmed"
PROVENANCE_INVALID = "invalid"

#: 新規カード作成(VerifiedNewCard生成)を許可するprovenance。
CREATABLE_PROVENANCES = (PROVENANCE_ARTICLE_JAPANESE_NAME, PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION)

RESOLUTION_EXISTING_CARD = "existing_card"
RESOLUTION_CREATABLE_FROM_ARTICLE_JAPANESE_NAME = "creatable_from_article_japanese_name"
RESOLUTION_CREATABLE_FROM_HUMAN_CONFIRMATION = "creatable_from_human_confirmation"
RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME = "blocked_missing_japanese_name"
RESOLUTION_BLOCKED_MISSING_CONFIRMATION = "blocked_missing_confirmation"
RESOLUTION_BLOCKED_AMBIGUOUS_MATCH = "blocked_ambiguous_match"
RESOLUTION_BLOCKED_INVALID_MAPPING = "blocked_invalid_mapping"
RESOLUTION_BLOCKED_IDENTITY_CONFLICT = "blocked_identity_conflict"

#: resolution_status のうち、Notionへの新規作成を止めるべきもの。
BLOCKED_RESOLUTION_STATUSES = frozenset(
    {
        RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME,
        RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
        RESOLUTION_BLOCKED_AMBIGUOUS_MATCH,
        RESOLUTION_BLOCKED_INVALID_MAPPING,
        RESOLUTION_BLOCKED_IDENTITY_CONFLICT,
    }
)

#: CardDecision.action のうち、新規カード作成が安全性理由でブロックされたもの。
#: 既存の "ambiguous" / "error" とは別の理由(日本語名未確認等)で
#: 新規作成のみを止める(照合そのものはambiguousではない)。
BLOCKED_CREATION_ACTIONS = frozenset(
    {
        RESOLUTION_BLOCKED_MISSING_JAPANESE_NAME,
        RESOLUTION_BLOCKED_MISSING_CONFIRMATION,
        RESOLUTION_BLOCKED_INVALID_MAPPING,
        RESOLUTION_BLOCKED_IDENTITY_CONFLICT,
    }
)


@dataclass(frozen=True)
class ConfirmationSource:
    """人間確認済み日本語名の出典。

    単なる任意文字列を「安全」とみなさないよう、種別(type)を必須にする。
    """

    type: str
    reference: str | None = None

    def __post_init__(self) -> None:
        if not self.type:
            raise DeckCardValidationError("ConfirmationSource.type が空です。")

    def to_dict(self) -> dict:
        return {"type": self.type, "reference": self.reference}


@dataclass(frozen=True)
class VerifiedNewCard:
    """新規カード作成の書き込み境界(未検証のDeckCardを直接渡さないための型)。

    CardRepository.create_card() はこの型のみを受け取る。name_ja は必ず
    確認済み(provenanceがCREATABLE_PROVENANCESのいずれか)であることを
    コンストラクタで再検証する。display_name のような英語名フォールバックは
    持たない(安全判定に使えないようにするため意図的に持たせていない)。
    """

    name_ja: str
    name_en: str | None
    provenance: str
    confirmation_source: ConfirmationSource | None
    source_url: str
    source_reference: str | None
    stable_key: str
    quantity: int
    is_commander: bool

    def __post_init__(self) -> None:
        if not self.name_ja:
            raise DeckCardValidationError("VerifiedNewCard.name_ja が空です(安全機構違反)。")
        if self.provenance not in CREATABLE_PROVENANCES:
            raise DeckCardValidationError(
                f"VerifiedNewCard.provenance が新規作成を許可しない値です: {self.provenance!r}"
            )
        if (
            self.provenance == PROVENANCE_EXPLICIT_HUMAN_CONFIRMATION
            and self.confirmation_source is None
        ):
            raise DeckCardValidationError(
                "explicit_human_confirmationにはconfirmation_sourceが必須です。"
            )


@dataclass(frozen=True)
class CardResolution:
    """1カード(1デッキ内での出現)の解決結果(import/verify/マニフェスト共通)。"""

    article_url: str
    deck_name: str
    quantity: int
    is_commander: bool
    name_en: str | None
    name_ja: str | None
    provenance: str | None
    confirmation_source: ConfirmationSource | None
    source_reference: str | None
    stable_key: str
    existing_page_id: str | None
    existing_candidate_page_ids: list[str]
    resolution_status: str
    block_reason: str | None = None
    verified_card: VerifiedNewCard | None = None

    @property
    def is_blocked(self) -> bool:
        return self.resolution_status in BLOCKED_RESOLUTION_STATUSES


@dataclass(frozen=True)
class CardDecision:
    """1カードに対するNotion登録計画(dry-run/適用で共有する)。

    action は以下のいずれか:
    - "create": カードDBに新規ページを作成する(resolutionのprovenanceが
      article_japanese_name または explicit_human_confirmationの場合のみ)
    - "relation_update": 既存カードに採用デッキのリレーションを追加する(所持もtrue化しうる)
    - "unchanged": 既存カードで変更不要(既にリレーション済み・所持済み)
    - "ambiguous": カードDB内で候補が複数あり一意に決定できない
    - "error": その他の理由で処理できない
    - BLOCKED_CREATION_ACTIONS のいずれか: 新規カードだが日本語名が未確認等の
      理由でNotionへの作成を止める(models.BLOCKED_CREATION_ACTIONS参照)
    """

    card: DeckCard
    action: str
    existing: ExistingCard | None = None
    detail: str = ""
    owned_will_change: bool = False
    override_used: str | None = None
    #: 新規作成判定の根拠(action=="create" または BLOCKED_CREATION_ACTIONS のときのみ設定)。
    resolution: CardResolution | None = None

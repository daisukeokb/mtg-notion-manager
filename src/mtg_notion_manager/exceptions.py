class MtgNotionManagerError(Exception):
    """このツール全体の基底例外。"""


class FetchError(MtgNotionManagerError):
    """外部ページの取得に失敗した。"""


class ParseError(MtgNotionManagerError):
    """ページ内容の解析に失敗した。"""


class UnsupportedSourceError(MtgNotionManagerError):
    """対応していないURL(サイト)が指定された。"""


class MultipleDecksFoundError(MtgNotionManagerError):
    """1ページに複数デッキが含まれており、MVPでは非対応。"""


class MappingError(MtgNotionManagerError):
    """発売セット名・色名などがNotionの既存選択肢にマッピングできない。"""


class NotionAPIError(MtgNotionManagerError):
    """Notion APIの呼び出しに失敗した。"""


class DeckCardValidationError(MtgNotionManagerError):
    """カード1件の抽出データが不正(カード名が空、枚数が1未満など)。"""


class DeckCountMismatchError(MtgNotionManagerError):
    """デッキの合計枚数が100枚(統率者戦の規定枚数)と一致しない。"""


class AmbiguousCardMatchError(MtgNotionManagerError):
    """カードDB内で複数の候補と一致し、一意に決定できない。"""


class CardMatchOverrideError(MtgNotionManagerError):
    """card_match_overrides.jsonの設定が不正、または指定page_idが候補内に存在しない。"""


class IntentionalDuplicateConfigError(MtgNotionManagerError):
    """intentional_duplicate_cards.jsonの設定が不正(JSON不正・必須キー欠落・矛盾する設定など)。"""


class DeckPageMappingConfigError(MtgNotionManagerError):
    """デッキページマッピング設定(--deck-page-map)が不正、または対象記事と一致しない。"""


class ConflictError(MtgNotionManagerError):
    """重複統合時、複数レコード間で属性値が競合し自動統合できない。"""


class RepresentativeSelectionError(MtgNotionManagerError):
    """重複統合時、代表レコードを優先順位だけでは一意に決定できない。"""


class SchemaMigrationError(MtgNotionManagerError):
    """Notionデータベースのスキーマ変更に失敗した、または前提条件を満たさない。"""

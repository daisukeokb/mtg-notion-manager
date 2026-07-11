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

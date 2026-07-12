"""カード名の正規化(重複判定・集計で使う比較キーの生成)。"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")
_SLASH_SEPARATOR_RE = re.compile(r"\s*//\s*")


def normalize_card_name(name: str) -> str:
    """カード名を比較キー用に正規化する(表示用の名前はそのまま保持すること)。

    - Unicode正規化(NFKC): 全角英数・全角スペースなどを統一
    - 前後・連続空白の圧縮
    - 両面カード区切り(//)前後の空白表記ゆれを統一
    - 大文字小文字を無視(casefold)
    """
    normalized = unicodedata.normalize("NFKC", name)
    normalized = normalized.strip()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = _SLASH_SEPARATOR_RE.sub(" // ", normalized)
    return normalized.casefold()

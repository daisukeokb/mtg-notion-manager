"""抽出元サイトの表記とNotion「MTG統率者DB」の選択肢を対応付けるマッピング表。

未知の値は絶対にNotionへ書き込まず MappingError を送出する。
新しいセット・見慣れない色表記が出てきた場合は、このファイルに追記してから
再実行すること(Notion側のselect選択肢を自動追加することはしない)。
"""

from __future__ import annotations

from mtg_notion_manager.exceptions import MappingError

# Notion「MTG統率者DB」>「発売セット」の既存選択肢(2026-07-11 時点で実測、
# 2026-07-11 に「イニストラード：真紅の契り」をNotion側で手動追加・反映)。
VALID_SET_NAMES: frozenset[str] = frozenset(
    {
        "マーベル スーパー・ヒーローズ",
        "モダンホライゾン3",
        "ブルームバロウ",
        "ダスクモーン：戦慄の館",
        "指輪物語：中つ国の伝承",
        "Fallout",
        "サンダー・ジャンクションの無法者",
        "エルドレインの森",
        "ストリクスヘイヴン",
        "ストリクスヘイヴンの秘密",
        "ローウィンの昏明",
        "タルキール：龍嵐録",
        "イニストラード：真紅の契り",
    }
)

# Notion「MTG統率者DB」>「色」の既存選択肢。
VALID_COLORS: frozenset[str] = frozenset({"白", "青", "黒", "赤", "緑", "無色"})

# magic.wizards.com の <deck-list set="XXX"> 属性(公式3文字セットコード)
# → Notionの発売セット名。
#
# 実測で確認済みのものにはコメントなし。未検証のものは要確認コメント付き。
# 誤っていても normalize_set_name() が MappingError を送出するだけで、
# 誤ったレコードがNotionに書き込まれることはない。
WIZARDS_SET_CODE_MAP: dict[str, str] = {
    "BLB": "ブルームバロウ",  # 実測確認済み(Animated Army記事)
    "MH3": "モダンホライゾン3",
    "DSK": "ダスクモーン：戦慄の館",
    "LTR": "指輪物語：中つ国の伝承",
    "OTJ": "サンダー・ジャンクションの無法者",
    "WOE": "エルドレインの森",
    "STX": "ストリクスヘイヴン",
    "TDM": "タルキール：龍嵐録",
    "PIP": "Fallout",
    # TODO: 要確認。実際のセットコードを確認してから使うこと。
    # "SPM": "マーベル スーパー・ヒーローズ",
    # "???": "ストリクスヘイヴンの秘密",
    # "???": "ローウィンの昏明",
}

# 英語の色名(magic.wizards.com の figcaption 等) → Notionの色名。
COLOR_EN_TO_JA: dict[str, str] = {
    "white": "白",
    "blue": "青",
    "black": "黒",
    "red": "赤",
    "green": "緑",
    "colorless": "無色",
}


def normalize_set_name(raw: str) -> str:
    """抽出したセット名/セットコードをNotionの選択肢名に正規化する。

    以下の順で解決を試みる:
    1. 既にNotionの選択肢名と完全一致する(mtg-jp.comの記事タイトル由来)
    2. Wizards公式の3文字セットコードとして解決できる

    どちらでも解決できない場合は MappingError を送出する。
    """
    candidate = raw.strip()
    if candidate in VALID_SET_NAMES:
        return candidate

    code = candidate.upper()
    if code in WIZARDS_SET_CODE_MAP:
        mapped = WIZARDS_SET_CODE_MAP[code]
        if mapped not in VALID_SET_NAMES:
            raise MappingError(
                f"セットコード '{code}' のマッピング先 '{mapped}' が"
                " Notionの発売セット選択肢に存在しません。mapping.py を確認してください。"
            )
        return mapped

    raise MappingError(
        f"発売セット '{raw}' をNotionの選択肢にマッピングできません。"
        " mapping.py の WIZARDS_SET_CODE_MAP または VALID_SET_NAMES を確認・追記してください。"
    )


def normalize_colors(raw_colors: list[str]) -> list[str]:
    """色トークンのリストをNotionの色名リストに正規化する。

    各トークンは以下のいずれかを想定する:
    - 既にNotionの色名そのもの(例: "赤")
    - 英語の色名(例: "Red")

    1つでも解決できないトークンがあれば MappingError を送出する
    (一部だけ登録して残りを欠落させるような不完全な結果は返さない)。
    """
    resolved: list[str] = []
    unresolved: list[str] = []

    for token in raw_colors:
        candidate = token.strip()
        if candidate in VALID_COLORS:
            resolved.append(candidate)
            continue

        mapped = COLOR_EN_TO_JA.get(candidate.lower())
        if mapped is not None:
            resolved.append(mapped)
            continue

        unresolved.append(token)

    if unresolved:
        raise MappingError(
            f"色 {unresolved} をNotionの選択肢にマッピングできません。"
            " mapping.py の COLOR_EN_TO_JA または VALID_COLORS を確認・追記してください。"
        )

    # 順序を保ちつつ重複を除去する。
    seen: set[str] = set()
    deduped: list[str] = []
    for color in resolved:
        if color not in seen:
            seen.add(color)
            deduped.append(color)
    return deduped

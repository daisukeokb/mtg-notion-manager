from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from mtg_notion_manager.exceptions import MtgNotionManagerError


class ConfigError(MtgNotionManagerError):
    """.env の設定が不足している。"""


@dataclass(frozen=True)
class Config:
    notion_api_key: str
    commander_data_source_id: str

    @classmethod
    def load(cls, dotenv_path: Path | None = None) -> "Config":
        load_dotenv(dotenv_path=dotenv_path)

        api_key = os.environ.get("NOTION_API_KEY", "").strip()
        data_source_id = os.environ.get("NOTION_COMMANDER_DATA_SOURCE_ID", "").strip()

        missing = []
        if not api_key:
            missing.append("NOTION_API_KEY")
        if not data_source_id:
            missing.append("NOTION_COMMANDER_DATA_SOURCE_ID")
        if missing:
            raise ConfigError(
                f"必要な環境変数が設定されていません: {', '.join(missing)}"
                " (.env を確認してください。.env.example を参考に作成できます)"
            )

        return cls(notion_api_key=api_key, commander_data_source_id=data_source_id)

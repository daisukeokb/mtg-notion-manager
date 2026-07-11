from __future__ import annotations

from typing import Any

import httpx

from mtg_notion_manager.exceptions import NotionAPIError

API_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"


class NotionClient:
    """Notion REST API(data source対応)の薄いラッパー。"""

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=API_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_current_user(self) -> dict:
        """トークンに紐づくbotユーザー情報を取得する(認証確認用)。"""
        return self._request("GET", "/users/me")

    def get_data_source(self, data_source_id: str) -> dict:
        """データソースのスキーマ(プロパティ定義)を取得する。"""
        return self._request("GET", f"/data_sources/{data_source_id}")

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
        payload = {
            "filter": {
                "property": title_property,
                "title": {"equals": title},
            }
        }
        response = self._request(
            "POST", f"/data_sources/{data_source_id}/query", json=payload
        )
        return response.get("results", [])

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        return self._request("POST", "/pages", json=payload)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise NotionAPIError(
                f"Notion API呼び出しに失敗しました"
                f" ({exc.response.status_code}): {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise NotionAPIError(f"Notion APIへの接続に失敗しました: {exc}") from exc
        return response.json()

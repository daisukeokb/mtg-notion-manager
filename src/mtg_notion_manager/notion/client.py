from __future__ import annotations

import time
from typing import Any

import httpx

from mtg_notion_manager.exceptions import NotionAPIError

API_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

MAX_RETRIES = 5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class NotionClient:
    """Notion REST API(data source対応)の薄いラッパー。

    429/5xx・タイムアウトは指数バックオフ(Retry-Afterがあれば優先)で
    最大 MAX_RETRIES 回まで自動リトライする。
    """

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

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        """データソースのスキーマにプロパティを追加/変更する(データベース設計の変更)。

        既存プロパティは省略すれば影響を受けない。呼び出しは明示的な操作が
        必要な破壊的変更になりうるため、呼び出し側で確認を取ってから使うこと。
        """
        payload = {"properties": properties}
        return self._request("PATCH", f"/data_sources/{data_source_id}", json=payload)

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
        payload = {
            "filter": {
                "property": title_property,
                "title": {"equals": title},
            }
        }
        response = self._request("POST", f"/data_sources/{data_source_id}/query", json=payload)
        return response.get("results", [])

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        """データソースの全ページを取得する(ページング対応、フィルタなし)。"""
        results: list[dict] = []
        payload: dict[str, Any] = {"page_size": page_size}
        while True:
            response = self._request("POST", f"/data_sources/{data_source_id}/query", json=payload)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                break
            payload["start_cursor"] = response["next_cursor"]
        return results

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        """relationなど複数値プロパティの全件を取得する。

        ページ本体のプロパティ値は25件で打ち切られる(has_more)ため、
        それを超える場合はこの専用エンドポイントでページングする。
        """
        results: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if cursor is not None:
                params["start_cursor"] = cursor
            response = self._request(
                "GET", f"/pages/{page_id}/properties/{property_id}", params=params
            )
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
        return results

    def read_relation_ids(self, properties: dict, page_id: str, property_name: str) -> list[str]:
        """ページのプロパティ辞書からrelationの全ページIDを取得する(25件超はページングして取得)。

        properties はページ取得・クエリ結果にすでに含まれる`properties`辞書を渡す
        (ページ本体の値は25件で打ち切られる`has_more`のため、超える場合のみ
        get_page_property_item で追加取得する)。
        """
        prop = properties.get(property_name, {})
        relation = prop.get("relation", [])
        if not prop.get("has_more"):
            return [item["id"] for item in relation]

        property_id = prop.get("id")
        if not property_id:
            return [item["id"] for item in relation]
        items = self.get_page_property_item(page_id, property_id)
        return [
            item["relation"]["id"]
            for item in items
            if item.get("type") == "relation" and "relation" in item
        ]

    def update_page(self, page_id: str, properties: dict) -> dict:
        payload = {"properties": properties}
        return self._request("PATCH", f"/pages/{page_id}", json=payload)

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        return self._request("POST", "/pages", json=payload)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        attempt = 0
        while True:
            try:
                response = self._client.request(method, path, **kwargs)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    time.sleep(_retry_wait_seconds(exc.response, attempt))
                    attempt += 1
                    continue
                raise NotionAPIError(
                    f"Notion API呼び出しに失敗しました ({status}): {exc.response.text}"
                ) from exc
            except httpx.TimeoutException as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(_backoff_seconds(attempt))
                    attempt += 1
                    continue
                raise NotionAPIError(f"Notion APIへの接続がタイムアウトしました: {exc}") from exc
            except httpx.HTTPError as exc:
                raise NotionAPIError(f"Notion APIへの接続に失敗しました: {exc}") from exc
            else:
                return response.json()


def _retry_wait_seconds(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return _backoff_seconds(attempt)


def _backoff_seconds(attempt: int) -> float:
    return min(2.0**attempt, 30.0)

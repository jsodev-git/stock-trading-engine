"""Backend (Spring Boot) REST 클라이언트.

엔진 ↔ Backend 역할 분리:
- 실시간 명령/조회 (active bots 폴링, 봇 상태 변경) → 이 클라이언트로 REST 호출
- 분석·통계 데이터 (매매이력, 시그널, 수급) → 엔진이 DB 직접 쓰기
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from config import config

log = logging.getLogger(__name__)


class BackendClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or config.backend_url).rstrip("/")
        self.api_key = api_key or config.internal_api_key
        self._session = requests.Session()
        self._session.headers.update({"X-Internal-Api-Key": self.api_key})

    def get_active_bots(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/internal/bots/active"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", [])

    def update_bot_status(self, bot_id: int, status: str) -> None:
        url = f"{self.base_url}/api/internal/bots/{bot_id}/status"
        resp = self._session.put(url, params={"status": status}, timeout=10)
        resp.raise_for_status()

    def record_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/api/internal/trades"
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def update_target_stock_codes(self, bot_id: int, stock_codes: list[str]) -> None:
        url = f"{self.base_url}/api/internal/bots/{bot_id}/target-stock-codes"
        resp = self._session.put(url, json=stock_codes, timeout=10)
        resp.raise_for_status()

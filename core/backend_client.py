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

    def record_account_snapshot(self, payload: dict[str, Any]) -> None:
        """봇의 현재 잔고·보유종목 스냅샷을 Backend DB에 기록."""
        url = f"{self.base_url}/api/internal/account-snapshots"
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()

    def record_scan_batch(self, rank_by: str, entries: list[dict[str, Any]]) -> None:
        """스크리닝 결과 일괄 전송. rank_by: VOLUME / PRICE_CHANGE / TRADE_AMOUNT."""
        payload = {
            "rankBy": rank_by,
            "entries": [
                {
                    "stockCode": e["stock_code"],
                    "stockName": e.get("stock_name"),
                    "rankPosition": e["rank"],
                    "price": e.get("price", 0.0),
                    "changeRate": e.get("change_rate", 0.0),
                    "volume": e.get("volume", 0),
                    "tradeAmount": e.get("trade_amount", 0.0),
                }
                for e in entries
            ],
        }
        url = f"{self.base_url}/api/internal/scans"
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()

    def record_flow(self, stock_code: str, flow: dict[str, int]) -> None:
        """종목 수급 데이터 저장."""
        payload = {
            "stockCode": stock_code,
            "foreignNetQty": flow.get("foreign_net_qty", 0),
            "institutionNetQty": flow.get("institution_net_qty", 0),
            "individualNetQty": flow.get("individual_net_qty", 0),
            "programNetQty": flow.get("program_net_qty"),
        }
        url = f"{self.base_url}/api/internal/flows"
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()

    def upsert_position(self, bot_id: int, stock_code: str, stock_name: str | None,
                        added_qty: int, added_price: float, added_cost: float) -> None:
        url = f"{self.base_url}/api/internal/positions/upsert"
        payload = {
            "botId": bot_id,
            "stockCode": stock_code,
            "stockName": stock_name,
            "addedQuantity": added_qty,
            "addedPrice": added_price,
            "addedCost": added_cost,
        }
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()

    def reduce_position(self, bot_id: int, stock_code: str, sold_qty: int) -> None:
        url = f"{self.base_url}/api/internal/positions/reduce"
        payload = {"botId": bot_id, "stockCode": stock_code, "soldQuantity": sold_qty}
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()

    def record_signal(self, bot_id: int, signal: dict[str, Any]) -> None:
        """매매 시그널 저장."""
        payload = {
            "botId": bot_id,
            "stockCode": signal["stockCode"],
            "stockName": signal.get("stockName"),
            "action": signal["action"],
            "strength": signal["strength"],
            "reasons": signal.get("reasons", []),
            "price": signal.get("price"),
            "executed": signal.get("executed", False),
        }
        url = f"{self.base_url}/api/internal/signals"
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()

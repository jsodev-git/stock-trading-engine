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

    def record_pending_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        """잔고-차분 흐름 — 주문 직전 PENDING 상태로 INSERT.

        멱등성 키(clientOrderId) 필수. status는 호출 측에서 'PENDING' 강제.
        UNIQUE 제약 충돌 시 backend가 duplicate=True 반환 (중복 호출 안전).
        """
        payload = dict(payload)
        payload["status"] = "PENDING"
        return self.record_trade(payload)

    def update_trade_status(self, client_order_id: str, status: str, **fields: Any) -> dict[str, Any]:
        """PENDING → FILLED/DISCREPANCY/FAILED 전이.

        fields 가능한 키 (모두 optional): orderUuid, actualPrice, actualVolume,
        actualAmount, actualFee, actualProfitRate, actualProfitAmount.
        FILLED 전이 시 SELL이고 actualProfitAmount 있으면 backend가 ProfitRecord 누적.
        """
        url = f"{self.base_url}/api/internal/trades/by-client-id/{client_order_id}/status"
        payload: dict[str, Any] = {"status": status}
        for key in ("orderUuid", "actualPrice", "actualVolume", "actualAmount",
                    "actualFee", "actualProfitRate", "actualProfitAmount"):
            if key in fields and fields[key] is not None:
                payload[key] = fields[key]
        resp = self._session.patch(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_pending_trades(self, bot_id: int) -> list[dict[str, Any]]:
        """자동 복구용 — 봇의 PENDING 상태 trade 리스트 (오래된 것부터)."""
        url = f"{self.base_url}/api/internal/trades/pending"
        resp = self._session.get(url, params={"botId": bot_id}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

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

    def get_positions(self, bot_id: int) -> list[dict[str, Any]]:
        """봇의 보유 포지션 목록."""
        url = f"{self.base_url}/api/internal/positions"
        resp = self._session.get(url, params={"botId": bot_id}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def update_position_prices(self, bot_id: int, prices: list[dict[str, Any]]) -> None:
        """prices: [{"stockCode": "005930", "currentPrice": 73000}, ...]"""
        if not prices:
            return
        url = f"{self.base_url}/api/internal/positions/prices"
        resp = self._session.post(url, json={"botId": bot_id, "prices": prices}, timeout=10)
        resp.raise_for_status()

    def reconcile_positions(self, bot_id: int, positions: list[dict[str, Any]]) -> dict[str, int]:
        """브로커 잔고를 진실로 간주해 DB Position을 재동기화."""
        url = f"{self.base_url}/api/internal/positions/reconcile"
        resp = self._session.post(url, json={"botId": bot_id, "positions": positions}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_blacklist(self, account_type: str) -> list[str]:
        """해당 계좌 타입의 활성 매매불가 종목코드 리스트."""
        url = f"{self.base_url}/api/internal/blacklist"
        resp = self._session.get(url, params={"accountType": account_type}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def block_stock(self, stock_code: str, account_type: str, reason: str, hours: int = 24) -> None:
        url = f"{self.base_url}/api/internal/blacklist"
        resp = self._session.post(url, json={
            "stockCode": stock_code, "accountType": account_type,
            "reason": reason, "hours": hours,
        }, timeout=10)
        resp.raise_for_status()

    def get_recent_sold_tickers(self, bot_id: int, hours: int = 24) -> list[str]:
        """봇 기준 hours 이내 매도된 고유 티커 — 재매수 금지용."""
        url = f"{self.base_url}/api/internal/trades/recent-sold"
        resp = self._session.get(url, params={"botId": bot_id, "hours": hours}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_ticker_pnl(self, bot_id: int, days: int = 30) -> dict[str, float]:
        """봇별 최근 N일 종목별 누적 net_pnl. 학습 루프 — 저성과 감점용 (2026-05-06)."""
        url = f"{self.base_url}/api/internal/trades/ticker-pnl"
        resp = self._session.get(url, params={"botId": bot_id, "days": days}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {}) or {}

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

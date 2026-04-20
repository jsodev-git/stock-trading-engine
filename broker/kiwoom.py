"""키움 OpenAPI+ 브로커 구현 (스텁).

키움은 Windows COM 기반이라 실제 연결은 PyQt5 QAxWidget + pykiwoom이 필요.
현재는 인터페이스 구조만 잡고, 핵심 메서드는 단계적으로 실제 구현한다.

credentials 스키마: {"accountNo": "...", "accountPassword": "..."}
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import (
    AccountType,
    BaseBroker,
    Balance,
    BrokerName,
    Market,
    OrderResult,
    OrderSide,
)

log = logging.getLogger(__name__)


class KiwoomBroker(BaseBroker):
    """키움 OpenAPI+ 연동. Windows 전용."""

    name = BrokerName.KIWOOM

    def __init__(self, market: Market = Market.KR,
                 account_type: AccountType = AccountType.MOCK) -> None:
        self.market = market
        self.account_type = account_type
        self._connected = False
        self._account_no: Optional[str] = None
        # TODO: pykiwoom.kiwoom.Kiwoom() 인스턴스 보관

    def connect(self, credentials: dict[str, str]) -> None:
        account_no = credentials.get("accountNo")
        password = credentials.get("accountPassword")
        if not account_no or not password:
            raise ValueError("KIWOOM: accountNo / accountPassword 필수")

        log.info("키움 연결 시도 account=%s type=%s", account_no, self.account_type)
        # TODO: pykiwoom CommConnect, 비밀번호 자동 입력, 계좌 선택
        self._account_no = account_no
        self._connected = True

    def disconnect(self) -> None:
        log.info("키움 연결 해제")
        self._connected = False
        self._account_no = None

    def get_balance(self) -> Balance:
        self._ensure_connected()
        # TODO: opw00018 (계좌평가잔고내역요청)
        return Balance(
            cash=0.0, locked=0.0, total_eval=0.0,
            currency="KRW" if self.market == Market.KR else "USD",
            positions=[],
        )

    def get_current_price(self, stock_code: str) -> float:
        self._ensure_connected()
        # TODO: opt10001 (주식기본정보요청)
        return 0.0

    def place_buy(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        self._ensure_connected()
        log.info("매수 %s x%d @ %s", stock_code, quantity, price or "시장가")
        return OrderResult(order_id="", stock_code=stock_code, side=OrderSide.BUY,
                           quantity=quantity, price=price or 0.0, filled=False)

    def place_sell(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        self._ensure_connected()
        log.info("매도 %s x%d @ %s", stock_code, quantity, price or "시장가")
        return OrderResult(order_id="", stock_code=stock_code, side=OrderSide.SELL,
                           quantity=quantity, price=price or 0.0, filled=False)

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_connected()
        return False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("키움 브로커가 연결되지 않았습니다. connect() 먼저 호출하세요.")

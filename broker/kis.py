"""한국투자증권 (KIS Developers) 브로커 구현 (스텁).

KIS는 REST API 기반이라 OS 제약이 없다. 키움과 달리 AppKey/AppSecret로 OAuth 토큰 발급 후
해당 토큰으로 시세·주문 API를 호출한다.

credentials 스키마: {"appKey": "...", "appSecret": "...", "accountNo": "..."}
"""
from __future__ import annotations

import logging

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


class KISBroker(BaseBroker):
    """한국투자증권 KIS Developers REST 연동."""

    name = BrokerName.KIS

    BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
    BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"

    def __init__(self, market: Market = Market.KR,
                 account_type: AccountType = AccountType.MOCK) -> None:
        self.market = market
        self.account_type = account_type
        self._connected = False
        self._access_token: str | None = None
        self._app_key: str | None = None
        self._app_secret: str | None = None
        self._account_no: str | None = None

    @property
    def base_url(self) -> str:
        return self.BASE_URL_REAL if self.account_type == AccountType.REAL else self.BASE_URL_MOCK

    def connect(self, credentials: dict[str, str]) -> None:
        app_key = credentials.get("appKey")
        app_secret = credentials.get("appSecret")
        account_no = credentials.get("accountNo")
        if not app_key or not app_secret or not account_no:
            raise ValueError("KIS: appKey / appSecret / accountNo 필수")

        log.info("KIS 연결 시도 type=%s account=%s", self.account_type, account_no)
        # TODO: POST /oauth2/tokenP로 access_token 발급 후 보관
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self._connected = True

    def disconnect(self) -> None:
        log.info("KIS 연결 해제")
        self._connected = False
        self._access_token = None

    def get_balance(self) -> Balance:
        self._ensure_connected()
        # TODO: /uapi/domestic-stock/v1/trading/inquire-balance
        return Balance(
            cash=0.0, locked=0.0, total_eval=0.0,
            currency="KRW" if self.market == Market.KR else "USD",
            positions=[],
        )

    def get_current_price(self, stock_code: str) -> float:
        self._ensure_connected()
        # TODO: /uapi/domestic-stock/v1/quotations/inquire-price
        return 0.0

    def place_buy(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        self._ensure_connected()
        log.info("[KIS] 매수 %s x%d @ %s", stock_code, quantity, price or "시장가")
        # TODO: /uapi/domestic-stock/v1/trading/order-cash
        return OrderResult(order_id="", stock_code=stock_code, side=OrderSide.BUY,
                           quantity=quantity, price=price or 0.0, filled=False)

    def place_sell(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        self._ensure_connected()
        log.info("[KIS] 매도 %s x%d @ %s", stock_code, quantity, price or "시장가")
        return OrderResult(order_id="", stock_code=stock_code, side=OrderSide.SELL,
                           quantity=quantity, price=price or 0.0, filled=False)

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_connected()
        return False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("KIS 브로커가 연결되지 않았습니다. connect() 먼저 호출하세요.")

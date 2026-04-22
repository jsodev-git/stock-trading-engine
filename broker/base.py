"""브로커 추상 인터페이스.

다중 증권사 지원을 위한 공통 계약. 새 브로커 추가 시:
1. 이 파일을 상속한 새 파일 작성 (예: `kis.py`)
2. `factory.py`의 `get_broker()` 매핑에 등록
3. Backend `Broker` enum에도 동일 이름 추가
4. Frontend `Broker` 타입·라벨·계좌 연결 폼 추가
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    KR = "KR"
    US = "US"


class AccountType(str, Enum):
    MOCK = "MOCK"
    REAL = "REAL"


class BrokerName(str, Enum):
    KIWOOM = "KIWOOM"
    KIS = "KIS"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderResult:
    order_id: str
    stock_code: str
    side: OrderSide
    quantity: int
    price: float
    filled: bool
    raw: dict | None = None


@dataclass
class OrderFill:
    """주문의 실제 체결 결과. 시장가 주문은 요청가와 체결가가 다르므로
    사후에 조회해 체결 평균가·금액을 확정한다."""
    order_id: str
    stock_code: str
    filled_quantity: int
    avg_fill_price: float      # 평균 체결가 (체결금액/수량)
    total_fill_amount: float   # 실제 체결금액 (수수료·세 제외)


@dataclass
class Position:
    stock_code: str
    stock_name: str
    quantity: int
    avg_price: float
    current_price: float

    @property
    def eval_amount(self) -> float:
        return self.quantity * self.current_price

    @property
    def profit_amount(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def profit_rate(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price


@dataclass
class Balance:
    cash: float
    locked: float
    total_eval: float
    currency: str
    positions: list[Position]


class BaseBroker(ABC):
    """모든 브로커 구현이 따라야 하는 계약."""

    name: BrokerName
    market: Market
    account_type: AccountType

    @abstractmethod
    def connect(self, credentials: dict[str, str]) -> None:
        """브로커별 필요한 인증 정보를 credentials dict로 받아 연결.

        각 브로커가 기대하는 credentials 스키마:
        - KIWOOM: {"accountNo": "...", "accountPassword": "..."}
        - KIS:    {"appKey": "...", "appSecret": "...", "accountNo": "..."}
        """

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def get_balance(self) -> Balance: ...

    @abstractmethod
    def get_current_price(self, stock_code: str) -> float: ...

    @abstractmethod
    def place_buy(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        """price=None이면 시장가."""

    @abstractmethod
    def place_sell(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    def get_order_fill(self, order_id: str, stock_code: str) -> OrderFill | None:
        """주문의 실제 체결 정보를 조회. 미구현 브로커는 None 반환.

        시장가 체결가 vs 요청가 괴리로 인한 집계 오차를 없애기 위해 사용.
        """
        return None

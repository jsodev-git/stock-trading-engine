"""수수료·거래세 계산 모듈.

국내 주식 거래 비용 구조:
- 증권사 수수료 (매수/매도 양쪽)
- 증권거래세 (매도 시만, 시장별 차등)
  - KOSPI: 0.18% (증권거래세) + 0.15%(농어촌특별세) = 0.23% (*2026년 기준)
  - KOSDAQ: 0.18%
- 모의투자는 수수료·세금 없음

이 모듈은:
1. 매수 실질 비용 (체결금액 × (1 + 수수료율))
2. 매도 실질 수취 (체결금액 × (1 - 수수료율 - 세율))
3. 매매 전 예상 순수익률이 수수료 왕복비용을 넘는지 필터
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StockMarket(str, Enum):
    """KRX 세부 시장. 세율이 다름."""
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


@dataclass(frozen=True)
class FeeSchedule:
    buy_fee_rate: float        # 매수 수수료율
    sell_fee_rate: float       # 매도 수수료율
    transaction_tax_rate: float  # 매도 시 거래세 (매수에는 없음)


# 기본 스케줄 — 모의/실거래·시장별 테이블
_SCHEDULES: dict[tuple[str, str], FeeSchedule] = {
    # 모의투자는 수수료·세금 없음 (KIS 모의투자 규정 기준)
    ("MOCK", "KOSPI"): FeeSchedule(0.0, 0.0, 0.0),
    ("MOCK", "KOSDAQ"): FeeSchedule(0.0, 0.0, 0.0),
    # 실계좌 — KIS Developers 기본 (증권사별 약간 다름)
    ("REAL", "KOSPI"): FeeSchedule(0.00015, 0.00015, 0.0023),
    ("REAL", "KOSDAQ"): FeeSchedule(0.00015, 0.00015, 0.0018),
}


def _schedule(account_type: str, market: str = "KOSPI") -> FeeSchedule:
    key = (account_type.upper(), market.upper())
    return _SCHEDULES.get(key) or FeeSchedule(0.0, 0.0, 0.0)


def buy_cost(price: float, quantity: int, account_type: str,
             stock_market: str = "KOSPI") -> tuple[float, float]:
    """매수 실질 투입금액 + 수수료 반환. (total_cost, fee)"""
    s = _schedule(account_type, stock_market)
    gross = price * quantity
    fee = gross * s.buy_fee_rate
    return gross + fee, fee


def sell_proceeds(price: float, quantity: int, account_type: str,
                  stock_market: str = "KOSPI") -> tuple[float, float]:
    """매도 실질 수취금액 + 총 비용 (수수료+세) 반환. (net_receive, total_cost)"""
    s = _schedule(account_type, stock_market)
    gross = price * quantity
    fee = gross * s.sell_fee_rate
    tax = gross * s.transaction_tax_rate
    return gross - fee - tax, fee + tax


def net_pnl(avg_price: float, sell_price: float, quantity: int,
            initial_total_cost: float, account_type: str,
            stock_market: str = "KOSPI") -> tuple[float, float]:
    """실현 순손익(수수료·세 차감) + 총 비용 반환. (net_pnl, total_fee_and_tax)"""
    net_receive, sell_cost = sell_proceeds(sell_price, quantity, account_type, stock_market)
    # initial_total_cost는 원가 전체(수수료 포함). quantity가 부분 매도면 비례 적용
    # 이 모듈은 단순화를 위해 전량 매도 기준으로만 정확; 부분 매도는 Position 쪽에서 처리
    return net_receive - initial_total_cost, sell_cost


def round_trip_cost_rate(account_type: str, stock_market: str = "KOSPI") -> float:
    """매수·매도 왕복 총 비용률 (수수료 2번 + 세)."""
    s = _schedule(account_type, stock_market)
    return s.buy_fee_rate + s.sell_fee_rate + s.transaction_tax_rate


def is_profitable_target(
    entry_price: float,
    target_price: float,
    account_type: str,
    stock_market: str = "KOSPI",
    min_multiple: float = 2.0,
) -> tuple[bool, float]:
    """예상 목표가로 매도 시 수수료 × min_multiple보다 순수익이 큰가?

    Returns:
        (is_worth, expected_net_rate)
    """
    if entry_price <= 0:
        return False, 0.0
    cost_rate = round_trip_cost_rate(account_type, stock_market)
    gross_rate = (target_price - entry_price) / entry_price
    net_rate = gross_rate - cost_rate
    return net_rate >= cost_rate * min_multiple, net_rate

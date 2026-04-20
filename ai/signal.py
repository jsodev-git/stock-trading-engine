"""매매 시그널 생성 — 스캔·수급 데이터를 결합한 규칙 기반 1차 구현.

점수화 규칙 (KRX 기준, 단순 합산 후 0~1 정규화):
- 거래량 상위 내 랭킹 (높을수록 가산)
- 등락률 상위 내 랭킹 (높을수록 가산)
- 등락률 자체 (+3% 이상 가산, 상한)
- 외인 순매수 양수 (+가산, 로그 스케일)
- 기관 순매수 양수 (+가산)
- 개인 순매수 음수 (= 세력이 받아주는 중, 가산 조건부)

투자성향별 threshold (BUY 임계치):
- AGGRESSIVE: 0.35 (적극 진입)
- MODERATE: 0.50
- CONSERVATIVE: 0.65

SELL 시그널은 보유 중인 종목에 대해 다음 조건:
- 등락률이 stopLossRate 도달
- 등락률이 takeProfitRate 도달
- 급격한 외인/기관 순매도
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Signal:
    stock_code: str
    stock_name: str
    action: str            # BUY / SELL / HOLD
    strength: float        # 0.0 ~ 1.0
    reasons: list[str]
    price: float


def _log_scale(qty: int, cap: int = 100_000) -> float:
    """순매수 수량을 0~1 사이 로그 점수로 변환 (cap 이상이면 1.0)."""
    if qty <= 0:
        return 0.0
    return min(1.0, math.log10(qty + 1) / math.log10(cap + 1))


def score_buy_candidate(
    stock_code: str,
    stock_name: str,
    price: float,
    change_rate: float,          # 등락률, 0.03 = +3%
    volume_rank: int | None,     # 거래량 상위 랭킹 (1이 최고), None이면 순위 없음
    price_rank: int | None,      # 등락률 상위 랭킹
    foreign_net: int,
    institution_net: int,
    individual_net: int,
    ranker_size: int = 30,
) -> Signal:
    """매수 후보 점수화 → Signal.

    KRX 가격제한폭이 ±30%라 상한가/하한가 근접 종목은 매수 제외.
    (상한가: 체결 거의 안 됨 + 추가 상승 여지 없음 / 하한가: 급락 중)
    """
    if change_rate >= 0.29:
        return Signal(
            stock_code=stock_code, stock_name=stock_name,
            action="HOLD", strength=0.0,
            reasons=[f"상한가 근접 +{change_rate*100:.2f}% (매수 제외)"],
            price=price,
        )
    if change_rate <= -0.29:
        return Signal(
            stock_code=stock_code, stock_name=stock_name,
            action="HOLD", strength=0.0,
            reasons=[f"하한가 근접 {change_rate*100:.2f}% (매수 제외)"],
            price=price,
        )

    reasons: list[str] = []
    score = 0.0

    # 1) 거래량 랭킹
    if volume_rank and volume_rank <= ranker_size:
        s = (ranker_size - volume_rank + 1) / ranker_size * 0.20
        score += s
        reasons.append(f"거래량 {volume_rank}위")

    # 2) 등락률 랭킹
    if price_rank and price_rank <= ranker_size:
        s = (ranker_size - price_rank + 1) / ranker_size * 0.20
        score += s
        reasons.append(f"등락률 {price_rank}위")

    # 3) 등락률 자체 (상승만, 3% 이상 가산, 10% 이상 포화)
    if change_rate > 0.03:
        s = min(0.15, (change_rate - 0.03) * 2)
        score += s
        reasons.append(f"등락률 +{change_rate*100:.2f}%")
    elif change_rate < -0.03:
        score -= 0.10
        reasons.append(f"등락률 {change_rate*100:.2f}% (감점)")

    # 4) 외인 순매수 (세력 매집)
    if foreign_net > 0:
        s = _log_scale(foreign_net) * 0.20
        score += s
        reasons.append(f"외인 +{foreign_net:,}주")
    elif foreign_net < -10_000:
        score -= 0.10
        reasons.append(f"외인 {foreign_net:,}주 (순매도)")

    # 5) 기관 순매수
    if institution_net > 0:
        s = _log_scale(institution_net) * 0.15
        score += s
        reasons.append(f"기관 +{institution_net:,}주")
    elif institution_net < -10_000:
        score -= 0.05
        reasons.append(f"기관 {institution_net:,}주 (순매도)")

    # 6) 개인 순매도 = 외인·기관이 받아가는 중 (보너스)
    if individual_net < 0 and (foreign_net > 0 or institution_net > 0):
        score += 0.10
        reasons.append("개인 순매도 (수급 흡수)")

    # 0 ~ 1 클램프
    strength = max(0.0, min(1.0, score))
    action = "BUY" if strength > 0 else "HOLD"

    return Signal(
        stock_code=stock_code,
        stock_name=stock_name,
        action=action,
        strength=strength,
        reasons=reasons,
        price=price,
    )


def should_sell_position(
    avg_price: float,
    current_price: float,
    stop_loss_rate: float,     # 음수 (-0.02)
    take_profit_rate: float,   # 양수 (+0.08)
) -> Signal | None:
    """보유 포지션에 대한 SELL 조건 평가."""
    if avg_price <= 0:
        return None
    rate = (current_price - avg_price) / avg_price

    reasons: list[str] = []
    strength = 0.0
    trigger = False

    if rate <= stop_loss_rate:
        trigger = True
        strength = 0.9
        reasons.append(f"손절 도달 {rate*100:.2f}%")
    elif rate >= take_profit_rate:
        trigger = True
        strength = 0.8
        reasons.append(f"익절 도달 +{rate*100:.2f}%")

    if not trigger:
        return None

    return Signal(
        stock_code="",  # 호출자가 채움
        stock_name="",
        action="SELL",
        strength=strength,
        reasons=reasons,
        price=current_price,
    )

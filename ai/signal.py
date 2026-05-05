"""매매 시그널 생성 — 데이터 분석 기반 재설계 (v2).

기존(v1)은 "거래량 상위 + 등락률 상위 + 수급" 단순 합산이었고,
실제 데이터 분석 결과 **시그널 강도와 수익률이 역상관** (0.80+ 고강도 -7.35%).
원인: "이미 많이 오른 종목"을 추격해 꼭대기 잡는 구조.

재설계 원칙 (축적 데이터 기반):
- 과열(+10% 이상) 종목은 강제 제외 — 대부분 당일 꺾임
- 등락률 sweet spot 1~5% 에 가산, 5~10% 는 약한 가산(추격 주의)
- 5일 SMA 근접도 — 근접할수록 가산, 멀수록 감점 (SMA(5) 대비 +5% 초과 시 감점)
- 외인+기관 동시 순매도 큰 감점 (실데이터에 없는 조합이어야 함)
- 거래량·등락률 동시 상위만 가산 (한쪽만은 약한 시그널)
- 수급 자체 가중치 축소 (실데이터 상관관계 약함)

Threshold: 단일값 0.40 (투자성향별 차이 없음, 성향은 자본배분 tradeRatio로)
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
    if qty <= 0:
        return 0.0
    return min(1.0, math.log10(qty + 1) / math.log10(cap + 1))


def score_buy_candidate(
    stock_code: str,
    stock_name: str,
    price: float,
    change_rate: float,
    volume_rank: int | None,
    price_rank: int | None,
    foreign_net: int,
    institution_net: int,
    individual_net: int,
    ranker_size: int = 30,
    daily_closes: list[float] | None = None,
    in_hot_theme: bool = False,
    historical_pnl: float | None = None,  # 2026-05-06: 직전 30일 net_pnl (학습 루프)
) -> Signal:
    """매수 후보 점수화 — v2 (데이터 분석 반영).

    hard filter:
    - ±29% 이상 (상/하한가 근접): 제외
    - +10% 이상 (당일 급등): 제외 (추격 매수 위험 — 분석상 대부분 꺾임)
    """
    # ─── Hard filters ───
    if change_rate >= 0.29:
        return Signal(stock_code, stock_name, "HOLD", 0.0,
                      [f"상한가 근접 +{change_rate*100:.2f}% skip"], price)
    if change_rate <= -0.29:
        return Signal(stock_code, stock_name, "HOLD", 0.0,
                      [f"하한가 근접 {change_rate*100:.2f}% skip"], price)
    if change_rate >= 0.10:
        return Signal(stock_code, stock_name, "HOLD", 0.0,
                      [f"과열 +{change_rate*100:.2f}% — 추격 매수 제외"], price)

    reasons: list[str] = []
    score = 0.0

    # 1) 등락률 sweet spot
    if 0.01 <= change_rate <= 0.05:
        score += 0.25
        reasons.append(f"건강한 상승 +{change_rate*100:.2f}%")
    elif 0.05 < change_rate < 0.10:
        score += 0.08
        reasons.append(f"강한 상승 +{change_rate*100:.2f}% (추격 주의)")
    elif -0.01 < change_rate < 0.01:
        score += 0.05
        reasons.append(f"보합 {change_rate*100:+.2f}%")
    elif change_rate <= -0.01:
        score -= 0.10
        reasons.append(f"하락 {change_rate*100:.2f}%")

    # 2) 5일 SMA 근접도 — 과열/과매수 억제
    sma5 = None
    if daily_closes and len(daily_closes) >= 5:
        sma5 = sum(daily_closes[-5:]) / 5
        if sma5 > 0:
            ratio = price / sma5
            if 0.98 <= ratio <= 1.03:
                score += 0.20
                reasons.append(f"SMA5 근접 (+{(ratio-1)*100:.1f}%)")
            elif 1.03 < ratio <= 1.05:
                score += 0.05
                reasons.append(f"SMA5 대비 +{(ratio-1)*100:.1f}%")
            elif ratio > 1.05:
                score -= 0.20
                reasons.append(f"SMA5 대비 +{(ratio-1)*100:.1f}% 과열 감점")
            elif ratio < 0.97:
                score -= 0.05
                reasons.append(f"SMA5 대비 {(ratio-1)*100:.1f}% 이탈")

    # 3) 거래량·등락률 동시 상위만 의미있는 신호
    if volume_rank and price_rank and volume_rank <= 15 and price_rank <= 15:
        score += 0.18
        reasons.append(f"거래·등락 동시 상위 (V{volume_rank}/P{price_rank})")
    elif volume_rank and volume_rank <= 10:
        score += 0.05
        reasons.append(f"거래량 {volume_rank}위")

    # 4) 외인 순매수 (약한 긍정)
    if foreign_net > 0:
        score += _log_scale(foreign_net) * 0.10
        reasons.append(f"외인 +{foreign_net:,}주")

    # 5) 기관 순매수
    if institution_net > 0:
        score += _log_scale(institution_net) * 0.08
        reasons.append(f"기관 +{institution_net:,}주")

    # 6) 외인+기관 동시 순매도 큰 감점 (위험 신호)
    if foreign_net < 0 and institution_net < 0:
        score -= 0.15
        reasons.append("외인+기관 동시 순매도")

    # 7) 개인 과매수 + 세력 매도 — 전형적 분산 덤핑 패턴
    if individual_net > 50_000 and (foreign_net < 0 or institution_net < 0):
        score -= 0.10
        reasons.append("개인 과매수 + 세력 매도 (덤핑 의심)")

    # 8) 당일 강세 테마 멤버 (여러 종목이 동일 흐름) — 테마 랠리 탑승
    if in_hot_theme:
        score += 0.18
        reasons.append("당일 테마 멤버 (동반 상승)")

    # 9) 학습 루프 (2026-05-06) — 직전 30일 동일 종목 net_pnl 기반 동적 감점.
    # 데이터 분석: 005880·049080·049480·114800 등 반복 손실 종목에 시그널이 자꾸 BUY 신호 발산.
    # 같은 종목 누적 손실이면 strength 깎아 같은 패턴 재발 차단.
    if historical_pnl is not None:
        if historical_pnl <= -50_000:
            score -= 0.20
            reasons.append(f"직전 30일 누적 -{abs(historical_pnl):,.0f}원 (저성과 감점)")
        elif historical_pnl < 0:
            score -= 0.10
            reasons.append(f"직전 30일 손실 {historical_pnl:,.0f}원")
        elif historical_pnl >= 50_000:
            # 우상향 종목은 약한 가산 (양방향 학습)
            score += 0.05
            reasons.append(f"직전 30일 누적 +{historical_pnl:,.0f}원 (우상향)")

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
    stop_loss_rate: float,
    take_profit_rate: float,
) -> Signal | None:
    """단순 FIXED 모드용 — AUTO는 exit_signal.decide_exit 사용."""
    if avg_price <= 0:
        return None
    rate = (current_price - avg_price) / avg_price

    if rate <= stop_loss_rate:
        return Signal("", "", "SELL", 0.9,
                      [f"손절 도달 {rate*100:.2f}%"], current_price)
    if rate >= take_profit_rate:
        return Signal("", "", "SELL", 0.8,
                      [f"익절 도달 +{rate*100:.2f}%"], current_price)
    return None

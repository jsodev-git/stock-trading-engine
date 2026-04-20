"""포지션 청산 판단 — AUTO/FIXED 두 모드.

Input:
- position: quantity, avg_price, peak_price, entry_at
- market: current_price, change_rate(%), volume_trend
- flow: foreign/institution/individual net
- bot config: investment_type, risk_mode, stop_loss_rate, take_profit_rate
- now: 현재 시각 (시간 exit 판단용)

Output:
- should_sell: bool
- reasons: list[str]
- urgency: 'high' / 'medium' / 'low' — 체결 시급도. high면 시장가, 그 외 지정가 고려

규칙 (AUTO 기준):
1. 절대 손절 (circuit breaker) — 투자성향별 -5%/-4%/-3%
2. 트레일링 스탑 — 진입 후 +5% 이상 찍은 뒤 고점 대비 -3%/-2%/-1.5% 되돌림
3. 손절 + 수급 이탈 (외인/기관 순매도) — stopLossRate 도달 + 수급 악화
4. 손절 + 수급 유지 — stopLossRate × 1.5까지 HOLD 가능
5. 익절 + 모멘텀 약화 — takeProfitRate 도달 + 거래량/수급 약화
6. 익절 + 모멘텀 강함 — HOLD + 트레일링 스탑으로 전환 (자동 반영)
7. 시간 만료 — 일정 시간 이상 보유 && 움직임 < 1%
8. 수급 급변 — 외인 대량 순매도(-50K주 이상) + 손실 중
9. 장 마감 15분 전 — CONSERVATIVE 한정 오버나잇 회피
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any


_PROFILE_PARAMS = {
    # (hard_stop, trailing_gap, hold_hours, close_before_market_end)
    "AGGRESSIVE":   (0.05, 0.030, 6, False),
    "MODERATE":     (0.04, 0.020, 4, False),
    "CONSERVATIVE": (0.03, 0.015, 2, True),
}

_MARKET_CLOSE_KST = time(15, 30)


@dataclass
class ExitDecision:
    should_sell: bool
    reasons: list[str]
    urgency: str           # 'high' | 'medium' | 'low'
    expected_net_gain: float | None = None  # 참고용


def decide_exit(
    *,
    # 포지션
    avg_price: float,
    quantity: int,
    peak_price: float,
    entry_at: datetime,
    # 시장
    current_price: float,
    volume_dropped: bool = False,   # 거래량 줄어드는지
    # 수급 (선택)
    foreign_net: int = 0,
    institution_net: int = 0,
    # 봇 설정
    investment_type: str = "MODERATE",
    risk_mode: str = "AUTO",        # "FIXED" or "AUTO"
    stop_loss_rate: float = -0.02,  # 예: -0.025 = -2.5%
    take_profit_rate: float = 0.08, # +0.08 = +8%
    now: datetime | None = None,
) -> ExitDecision:
    now = now or datetime.now()
    current_profit = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
    peak_profit = (peak_price - avg_price) / avg_price if avg_price > 0 else 0.0
    drawdown = (peak_price - current_price) / peak_price if peak_price > 0 else 0.0

    reasons: list[str] = []

    # FIXED 모드 — 단순 임계값
    if risk_mode.upper() == "FIXED":
        if current_profit <= stop_loss_rate:
            return ExitDecision(True, [f"손절 도달 {current_profit*100:.2f}%"], "high")
        if current_profit >= take_profit_rate:
            return ExitDecision(True, [f"익절 도달 +{current_profit*100:.2f}%"], "medium")
        return ExitDecision(False, [], "low")

    # AUTO 모드
    params = _PROFILE_PARAMS.get(investment_type.upper(), _PROFILE_PARAMS["MODERATE"])
    hard_stop, trailing_gap, hold_hours, close_before_end = params

    # 1) 절대 손절 (circuit breaker)
    if current_profit <= -hard_stop:
        return ExitDecision(
            True, [f"절대 손절 {current_profit*100:.2f}% (한계 {-hard_stop*100:.1f}%)"],
            "high", current_profit
        )

    # 2) 트레일링 스탑 — 진입 후 +5% 이상 찍은 후 되돌림
    if peak_profit >= 0.05 and drawdown >= trailing_gap:
        return ExitDecision(
            True,
            [f"트레일링 스탑 — 고점 +{peak_profit*100:.2f}%에서 -{drawdown*100:.2f}% 되돌림"],
            "high", current_profit,
        )

    # 3) 손절선 도달
    if current_profit <= stop_loss_rate:
        flow_bearish = (foreign_net < 0) or (institution_net < 0)
        if flow_bearish:
            reasons.append(f"손절 {current_profit*100:.2f}% + 수급 이탈")
            if foreign_net < 0:
                reasons.append(f"외인 {foreign_net:,}주 순매도")
            if institution_net < 0:
                reasons.append(f"기관 {institution_net:,}주 순매도")
            return ExitDecision(True, reasons, "high", current_profit)
        # 수급 유지 중이면 stopLossRate × 1.5까지 인내
        if current_profit <= stop_loss_rate * 1.5:
            return ExitDecision(
                True,
                [f"손절 강제 {current_profit*100:.2f}% (한계치 1.5배 도달)"],
                "high", current_profit,
            )
        # 아니면 HOLD
        return ExitDecision(False, [f"손절 {current_profit*100:.2f}%이지만 수급 유지 (HOLD)"], "low")

    # 4) 익절선 도달
    if current_profit >= take_profit_rate:
        momentum_weak = volume_dropped or (foreign_net < 0) or (institution_net < 0)
        if momentum_weak:
            reasons.append(f"익절 +{current_profit*100:.2f}% + 모멘텀 약화")
            if volume_dropped:
                reasons.append("거래량 감소")
            if foreign_net < 0:
                reasons.append(f"외인 {foreign_net:,}주")
            return ExitDecision(True, reasons, "medium", current_profit)
        # 모멘텀 강함 → 트레일링 스탑이 다음 사이클에 발동될 것 (HOLD)
        return ExitDecision(
            False,
            [f"익절 +{current_profit*100:.2f}% 도달했으나 모멘텀 강 (트레일링 HOLD)"],
            "low",
        )

    # 5) 수급 급변
    if foreign_net <= -50_000 and current_profit < 0:
        return ExitDecision(
            True,
            [f"외인 대량 순매도 {foreign_net:,}주 + 손실 {current_profit*100:.2f}%"],
            "high", current_profit,
        )

    # 6) 시간 만료
    elapsed = now - entry_at
    if elapsed >= timedelta(hours=hold_hours) and abs(current_profit) < 0.01:
        return ExitDecision(
            True,
            [f"시간 만료 {elapsed.total_seconds()/3600:.1f}h + 변동 거의 없음"],
            "medium", current_profit,
        )

    # 7) 장 마감 임박 (보수형만)
    if close_before_end:
        minutes_to_close = _minutes_to_market_close(now)
        if 0 < minutes_to_close <= 15:
            return ExitDecision(
                True,
                [f"장 마감 {minutes_to_close}분 전 — 보수형 오버나잇 회피"],
                "high", current_profit,
            )

    return ExitDecision(False, [], "low")


def _minutes_to_market_close(now: datetime) -> int:
    """남은 장 시간 (분). 이미 지났으면 음수, 주말이면 큰 수."""
    close_dt = now.replace(hour=_MARKET_CLOSE_KST.hour,
                           minute=_MARKET_CLOSE_KST.minute,
                           second=0, microsecond=0)
    return int((close_dt - now).total_seconds() / 60)

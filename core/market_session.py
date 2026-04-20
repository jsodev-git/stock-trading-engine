"""시장별 장 운영시간 판단.

KR (KRX): 평일 09:00 ~ 15:30 KST
US (NYSE/NASDAQ): 평일 23:30 ~ 06:00 KST (서머타임 시 22:30 ~ 05:00)
— 서머타임 처리는 US 구현 단계에서 정식 캘린더로 보강.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

KR_OPEN = time(9, 0)
KR_CLOSE = time(15, 30)

# US 평일 KST 기준 대략치 (서머타임 미반영 — 실제 구현 시 US/Eastern 기준 캘린더 적용)
US_OPEN = time(23, 30)
US_CLOSE = time(6, 0)


def _is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon-Fri


def is_market_open(market: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)

    market = market.upper()

    if market == "KR":
        if not _is_weekday(now):
            return False
        t = now.time()
        return KR_OPEN <= t <= KR_CLOSE

    if market == "US":
        # 야간장 (23:30~06:00)이므로 전일 야간 또는 당일 새벽 케이스
        t = now.time()
        if t >= US_OPEN:
            return _is_weekday(now)
        if t <= US_CLOSE:
            # 새벽 시간 — 전일이 평일이어야 장 오픈
            return _is_weekday(now - timedelta(days=1))
        return False

    raise ValueError(f"지원하지 않는 시장: {market}")


def next_open_at(market: str, now: datetime | None = None) -> datetime:
    """다음 시장 오픈 시각 (최대 7일 내 탐색)."""
    now = now or datetime.now(tz=KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)

    market = market.upper()
    open_time = KR_OPEN if market == "KR" else US_OPEN

    for offset in range(0, 8):
        candidate = (now + timedelta(days=offset)).replace(
            hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0
        )
        if candidate <= now:
            continue
        if _is_weekday(candidate):
            return candidate
    raise RuntimeError("7일 내 다음 장 오픈 시각을 찾지 못했습니다.")

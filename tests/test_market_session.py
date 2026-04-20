from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.market_session import is_market_open

KST = ZoneInfo("Asia/Seoul")


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=KST)


def test_kr_open_weekday_morning() -> None:
    # 2026-04-20 은 월요일
    assert is_market_open("KR", at(4, 20, 10, 0)) is True


def test_kr_closed_before_open() -> None:
    assert is_market_open("KR", at(4, 20, 8, 59)) is False


def test_kr_closed_after_close() -> None:
    assert is_market_open("KR", at(4, 20, 15, 31)) is False


def test_kr_closed_on_weekend() -> None:
    # 2026-04-25 은 토요일
    assert is_market_open("KR", at(4, 25, 10, 0)) is False


def test_us_open_evening() -> None:
    # 월요일 밤 23:30 KST → 미국 장 시작
    assert is_market_open("US", at(4, 20, 23, 30)) is True


def test_us_closed_between_sessions() -> None:
    assert is_market_open("US", at(4, 20, 12, 0)) is False

"""매매 시그널 생성 (스텁). 기술적 분석 + AI 점수 하이브리드로 매수/매도 신호를 만든다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    stock_code: str
    action: str       # BUY / SELL / HOLD
    strength: float   # 0.0 ~ 1.0
    reasons: list[str]


def generate(stock_code: str) -> Signal:
    # TODO: pandas/ta 지표 + 뉴스/테마 점수 결합
    return Signal(stock_code=stock_code, action="HOLD", strength=0.0, reasons=[])

"""뉴스 분석 (스텁). 종목별 최근 뉴스를 LLM/감성분석으로 점수화해 시그널에 반영."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NewsSentiment:
    stock_code: str
    score: float  # -1.0 ~ +1.0
    summary: str


def analyze(stock_code: str) -> NewsSentiment:
    # TODO: 뉴스 수집 → LLM 요약 → 감성 점수 산출
    return NewsSentiment(stock_code=stock_code, score=0.0, summary="")

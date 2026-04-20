"""국내 시장(KRX) 스크리너.

KIS API로 거래량/등락률 상위 종목을 가져와 Backend DB에 기록한다.
실행 중인 봇 중 KIS+KR 계좌를 가진 첫 번째 봇의 자격증명으로 호출.
"""
from __future__ import annotations

import logging
from typing import Iterable

from broker import get_broker
from broker.base import BaseBroker
from core.backend_client import BackendClient

log = logging.getLogger(__name__)


def _pick_kr_kis_broker(bots: Iterable[dict]) -> tuple[BaseBroker, dict] | None:
    """활성 봇 중 KRX + KIS 자격증명을 가진 첫 봇으로 브로커 생성."""
    for bot in bots:
        if bot.get("market") != "KR":
            continue
        if bot.get("broker") != "KIS":
            continue
        credentials = bot.get("credentials") or {}
        if not credentials.get("appKey"):
            continue
        broker = get_broker("KIS", market="KR", account_type=bot.get("accountType", "MOCK"))
        broker.connect(credentials)
        return broker, bot
    return None


def scan_kr_market(client: BackendClient, top_n: int = 30) -> None:
    """거래량 상위 + 등락률 상위를 Backend에 기록."""
    try:
        bots = client.get_active_bots()
    except Exception as e:
        log.warning("[scanner] 봇 목록 조회 실패: %s", e)
        return

    picked = _pick_kr_kis_broker(bots)
    if picked is None:
        log.debug("[scanner] KR+KIS 봇 없음 — skip")
        return
    broker, bot = picked

    try:
        # 거래량 상위
        try:
            volume_rows = broker.get_volume_rankers(top_n)
            if volume_rows:
                client.record_scan_batch("VOLUME", volume_rows)
                log.info("[scanner][VOLUME] %d건 저장", len(volume_rows))
        except Exception as e:
            log.warning("[scanner] 거래량 상위 실패: %s", e)

        # 등락률 상위
        try:
            price_rows = broker.get_price_change_rankers(top_n)
            if price_rows:
                client.record_scan_batch("PRICE_CHANGE", price_rows)
                log.info("[scanner][PRICE_CHANGE] %d건 저장", len(price_rows))
        except Exception as e:
            log.warning("[scanner] 등락률 상위 실패: %s", e)

        # 상위 교집합 종목에 대해 수급 수집 (세력 매집 판단용)
        # 거래량·등락률 양쪽에 모두 들어있는 종목은 단기 폭발적 관심을 받는 중
        volume_codes = {r["stock_code"] for r in (volume_rows or [])}
        price_codes = {r["stock_code"] for r in (price_rows or [])}
        hot_codes = list(volume_codes & price_codes)[:10]  # KIS rate limit 고려 최대 10개
        log.info("[scanner] 핫 종목 %d개 수급 수집", len(hot_codes))

        for code in hot_codes:
            try:
                flow = broker.get_investor_flow(code)
                client.record_flow(code, flow)
            except Exception as e:
                log.warning("[scanner][flow] %s 실패: %s", code, e)
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass

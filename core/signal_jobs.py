"""봇별 시그널 생성 잡.

동작:
1. Backend에서 실행 중 봇 목록 조회 (credentials 포함)
2. Backend `/api/signals/scan`은 memberId 필요한 API라 내부 DB 조회를 엔진이 직접 하지 않고,
   엔진은 scanner가 방금 저장한 스캔/수급을 다시 호출하는 대신, 로컬에서 결과를 전달받아
   곧장 점수 계산 후 Backend에 시그널 저장.

   → scan_and_signal_kr가 scan_kr_market의 결과를 반환하도록 하면 중복 호출 없음.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ai.signal import score_buy_candidate
from broker import get_broker
from broker.base import BaseBroker
from core.backend_client import BackendClient
from core.executor import execute_buy_for_bot, execute_exits_for_bot

# KIS는 초당 API 요청 수가 제한돼 있어 각 호출 사이 지연
_KIS_CALL_DELAY = 1.0

log = logging.getLogger(__name__)

# 투자성향별 BUY 임계값
_BUY_THRESHOLD = {
    "AGGRESSIVE": 0.35,
    "MODERATE": 0.50,
    "CONSERVATIVE": 0.65,
}


def _pick_kr_kis_broker(bots: list[dict]) -> BaseBroker | None:
    for bot in bots:
        if bot.get("market") != "KR" or bot.get("broker") != "KIS":
            continue
        creds = bot.get("credentials") or {}
        if not creds.get("appKey"):
            continue
        broker = get_broker("KIS", market="KR", account_type=bot.get("accountType", "MOCK"))
        broker.connect(creds)
        return broker
    return None


def scan_and_signal_kr(client: BackendClient, top_n: int = 30) -> None:
    """KR 시장 스캔 + 봇별 시그널 생성·저장.

    이 한 잡 안에서 스캔→수급→시그널까지 묶어 KIS rate limit을 최소화한다.
    """
    try:
        bots = client.get_active_bots()
    except Exception as e:
        log.warning("[signal_job] 봇 목록 조회 실패: %s", e)
        return

    kr_bots = [b for b in bots if b.get("market") == "KR"]
    if not kr_bots:
        log.debug("[signal_job] KR 봇 없음 — skip")
        return

    broker = _pick_kr_kis_broker(kr_bots)
    if broker is None:
        log.debug("[signal_job] KIS 브로커 없음 — skip")
        return

    try:
        # 1. 스크리너
        try:
            volume_rows = broker.get_volume_rankers(top_n)
            client.record_scan_batch("VOLUME", volume_rows)
            log.info("[signal_job] 거래량 상위 %d건 저장", len(volume_rows))
        except Exception as e:
            log.warning("[signal_job] 거래량 상위 실패: %s", e)
            volume_rows = []

        time.sleep(_KIS_CALL_DELAY)

        try:
            price_rows = broker.get_price_change_rankers(top_n)
            client.record_scan_batch("PRICE_CHANGE", price_rows)
            log.info("[signal_job] 등락률 상위 %d건 저장", len(price_rows))
        except Exception as e:
            log.warning("[signal_job] 등락률 상위 실패: %s", e)
            price_rows = []

        # 2. 후보 종목 (거래량 ∩ 등락률 교집합 상위 10개) + 수급
        volume_by_code: dict[str, dict] = {r["stock_code"]: r for r in volume_rows}
        price_by_code: dict[str, dict] = {r["stock_code"]: r for r in price_rows}
        hot_codes = list(set(volume_by_code) & set(price_by_code))

        # 등락률 상위 순으로 정렬
        hot_codes.sort(key=lambda c: price_by_code[c]["rank"])
        hot_codes = hot_codes[:10]  # KIS rate limit 고려

        log.info("[signal_job] 핫 종목 %d개 분석", len(hot_codes))

        time.sleep(_KIS_CALL_DELAY)  # price_rankers → flow 사이 지연

        # 수급 수집 대상 = hot_codes + 봇 보유 종목 (exit 판정에 필요)
        held_codes: set[str] = set()
        for bot in kr_bots:
            try:
                positions = client.get_positions(bot["id"])
                held_codes.update(p.get("stockCode") for p in positions if p.get("stockCode"))
            except Exception as e:
                log.debug("[signal_job] 봇 %s 포지션 조회 실패: %s", bot["id"], e)
        target_codes = list(dict.fromkeys(list(hot_codes) + list(held_codes)))[:15]  # rate limit 고려 15개
        log.debug("[signal_job] 수급 수집 대상 %d개 (핫 %d + 보유 %d)",
                  len(target_codes), len(hot_codes), len(held_codes))

        flow_by_code: dict[str, dict] = {}
        for code in target_codes:
            try:
                flow = broker.get_investor_flow(code)
                flow_by_code[code] = flow
                client.record_flow(code, flow)
            except Exception as e:
                log.warning("[signal_job] 수급 조회 실패 %s: %s", code, e)

        if not hot_codes:
            log.info("[signal_job] 핫 종목 없음, 시그널 생성 skip")
            return

        # 3. 봇별 시그널 생성 + 실행
        for bot in kr_bots:
            threshold = _BUY_THRESHOLD.get(bot.get("investmentType", "MODERATE"), 0.5)
            bot_id = bot["id"]
            generated = 0
            buy_candidates: list[dict[str, Any]] = []
            for code in hot_codes:
                v = volume_by_code.get(code) or {}
                p = price_by_code.get(code) or {}
                f = flow_by_code.get(code) or {
                    "foreign_net_qty": 0, "institution_net_qty": 0,
                    "individual_net_qty": 0,
                }
                signal = score_buy_candidate(
                    stock_code=code,
                    stock_name=v.get("stock_name") or p.get("stock_name") or "",
                    price=p.get("price") or v.get("price") or 0,
                    change_rate=p.get("change_rate") or v.get("change_rate") or 0,
                    volume_rank=v.get("rank"),
                    price_rank=p.get("rank"),
                    foreign_net=f.get("foreign_net_qty", 0),
                    institution_net=f.get("institution_net_qty", 0),
                    individual_net=f.get("individual_net_qty", 0),
                    ranker_size=top_n,
                )
                action = "BUY" if signal.strength >= threshold else "HOLD"
                payload: dict[str, Any] = {
                    "stockCode": signal.stock_code,
                    "stockName": signal.stock_name,
                    "action": action,
                    "strength": signal.strength,
                    "reasons": signal.reasons,
                    "price": signal.price,
                    "executed": False,
                }
                try:
                    client.record_signal(bot_id, payload)
                    generated += 1
                except Exception as e:
                    log.warning("[signal_job] 시그널 저장 실패 bot=%s %s: %s",
                                bot_id, code, e)

                if action == "BUY":
                    buy_candidates.append({
                        "stock_code": signal.stock_code,
                        "stock_name": signal.stock_name,
                        "price": signal.price,
                        "change_rate": p.get("change_rate") or 0,
                        "strength": signal.strength,
                        "reasons": signal.reasons,
                    })

            log.info("[signal_job][%s] %d개 시그널 (threshold=%.2f, BUY %d)",
                     bot.get("name"), generated, threshold, len(buy_candidates))

            # 실행 — 각 봇마다 자체 브로커 세션
            _execute_for_bot(client, bot, buy_candidates, flow_by_code)
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass


def _execute_for_bot(client: BackendClient, bot: dict,
                      buy_candidates: list[dict[str, Any]],
                      flow_by_code: dict[str, dict]) -> None:
    """봇 전용 브로커 세션으로 BUY/Exit 주문 수행."""
    bot_name = bot.get("name", str(bot["id"]))
    broker_name = bot.get("broker", "KIS")
    market = bot.get("market", "KR")
    account_type = bot.get("accountType", "MOCK")
    credentials = bot.get("credentials") or {}

    try:
        bot_broker = get_broker(broker_name, market=market, account_type=account_type)
        bot_broker.connect(credentials)
    except Exception as e:
        log.warning("[signal_job][%s] 브로커 연결 실패: %s", bot_name, e)
        return

    try:
        # 1. Exit 먼저 — 새 진입 전 청산부터
        execute_exits_for_bot(client, bot_broker, bot, flow_by_code)

        # 2. 진입 — 가용 현금 확인 후
        balance = None
        try:
            balance = bot_broker.get_balance()
        except Exception as e:
            log.warning("[signal_job][%s] 잔고 조회 실패, BUY skip: %s", bot_name, e)

        if balance and buy_candidates:
            execute_buy_for_bot(client, bot_broker, bot, buy_candidates, balance.cash)
    finally:
        try:
            bot_broker.disconnect()
        except Exception:
            pass

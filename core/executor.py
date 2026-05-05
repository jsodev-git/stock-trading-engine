"""매매 실행 — BUY 시그널을 주문으로, 보유 포지션을 exit 판단으로 연결.

설계:
- ENGINE_MODE=DRY: 주문 호출 없이 로그만 (테스트용)
- ENGINE_MODE=PAPER: MOCK 계좌에만 실제 주문 (안전 기본값)
- ENGINE_MODE=LIVE: MOCK + REAL 모두 실제 주문

동시성/중복 보호:
- 진입 전에 항상 Backend에서 현재 포지션 조회
- 이미 보유 중인 종목은 skip (추가 매수는 별도 로직에서 처리)
- maxPositions 도달 시 skip
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# KIS 초당 rate limit 대응
_ORDER_DELAY = 1.0
# 주문 후 체결가 조회까지 대기 시간 — 시장가는 즉시 체결되지만 조회 반영까지 여유 필요.
# 3회 시도(총 ~9s)로 여유있게 대기 — 조회 지연으로 폴백되는 비율을 최소화.
_FILL_LOOKUP_DELAYS = (1.5, 2.5, 4.0)
# 중복 매매 방지 TTL — 직전 체결 직후 다음 exit/buy 사이클이 KIS 잔고 반영 전에
# 동일 종목을 다시 건드리는 것을 막는다. signal_cycle 90s의 최소 6배 이상 잡아
# 연속 사이클 사이에 확실히 차단되도록.
_RECENT_TRADE_TTL = 600.0  # seconds (10분)

# 잔고-차분 검증 — 주문 직후 KIS 잔고에 반영되기까지 1~3초 지연.
# retry 3회로 eventual consistency 흡수, 여전히 변화 없으면 DISCREPANCY 또는 PENDING 유지.
# 2026-05-06: 1.5+3.0(총 4.5s) → 2.0+4.0+6.0(총 12s)으로 강화. KIS 모의계좌가 7번 연속 false
# negative 발생한 사고 후 (4/30 114800 7건 중복매수). 시간 비용 < 데이터 정합성.
_BALANCE_VERIFY_DELAYS = (2.0, 4.0, 6.0)
# 자동 복구 임계 — PENDING 상태가 이만큼 경과해도 잔고에 안 반영됐으면 FAILED 마킹.
# 너무 짧으면 KIS 지연 시 오탐, 너무 길면 다음 사이클 매매와 혼선. 5분이 합리.
_PENDING_FAIL_THRESHOLD_SEC = 300.0

from ai.exit_signal import decide_exit
from ai.fee import buy_cost, is_profitable_target, net_pnl, sell_proceeds
from broker.base import BaseBroker, Balance, OrderFill, OrderSide
from config import config
from core.backend_client import BackendClient

log = logging.getLogger(__name__)


@dataclass
class BalanceVerification:
    """잔고-차분 검증 결과.

    - matched=True: 변화량이 expected_delta와 정확히 일치. FILLED 전이 가능.
    - matched=False, error='no_change': 변화 없음 (주문 미반영 — DISCREPANCY 후보)
    - matched=False, error='qty_mismatch': 변화는 있는데 수량 다름 (DISCREPANCY)
    - matched=False, error='lookup_failed': 잔고 조회 자체 실패 (PENDING 유지 → 자동 복구로)
    """
    matched: bool
    actual_delta: int           # 실제 변화량 (BUY는 +, SELL은 -)
    actual_qty_after: int       # 검증 후 보유 수량
    actual_avg_price: float     # 검증 후 평균가 (BUY 보정용, SELL은 변화 없음)
    error: str | None = None


def _get_held_qty_from_balance(balance: Balance, stock_code: str) -> tuple[int, float]:
    """Balance에서 특정 종목의 (수량, 평균가) 추출. 없으면 (0, 0.0)."""
    for p in balance.positions:
        if p.stock_code == stock_code:
            return int(p.quantity), float(p.avg_price)
    return 0, 0.0


def _verify_balance_change(
    broker: BaseBroker, stock_code: str, prev_qty: int, expected_delta: int,
) -> BalanceVerification:
    """주문 후 잔고를 다시 조회해 변화량이 기대값과 일치하는지 검증.

    KIS는 주문 체결과 잔고 반영 사이에 1~3초 지연이 있을 수 있어
    재시도로 eventual consistency 흡수. 그래도 안 맞으면 호출 측이 PENDING 유지 또는 DISCREPANCY 결정.
    """
    last_qty, last_avg = prev_qty, 0.0
    for delay in _BALANCE_VERIFY_DELAYS:
        time.sleep(delay)
        try:
            balance = broker.get_balance()
        except Exception as e:
            log.debug("잔고 검증 조회 실패 %s (delay %.1fs): %s", stock_code, delay, e)
            continue
        cur_qty, cur_avg = _get_held_qty_from_balance(balance, stock_code)
        last_qty, last_avg = cur_qty, cur_avg
        actual_delta = cur_qty - prev_qty
        if actual_delta == expected_delta:
            return BalanceVerification(True, actual_delta, cur_qty, cur_avg)
        # 변화는 있는데 수량 다른 경우 — 부분체결 가능성. 한 번 더 wait해 본다.
        if actual_delta != 0 and actual_delta != expected_delta:
            log.debug("잔고 변화 부분일치 %s: prev=%d cur=%d delta=%d expected=%d (재시도)",
                      stock_code, prev_qty, cur_qty, actual_delta, expected_delta)
    # 마지막 측정 결과로 분류
    final_delta = last_qty - prev_qty
    if final_delta == expected_delta:
        return BalanceVerification(True, final_delta, last_qty, last_avg)
    if final_delta == 0:
        return BalanceVerification(False, 0, last_qty, last_avg, error="no_change")
    return BalanceVerification(False, final_delta, last_qty, last_avg, error="qty_mismatch")

# (bot_id, stock_code, action) → 체결 시각. 모듈 레벨에 저장해 사이클 간 공유.
_RECENT_TRADES: dict[tuple[int, str, str], float] = {}


def _mark_recent_trade(bot_id: int, code: str, action: str) -> None:
    _RECENT_TRADES[(bot_id, code, action)] = time.time()
    # 오래된 엔트리 청소 (메모리 누수 방지)
    cutoff = time.time() - _RECENT_TRADE_TTL * 2
    for key, ts in list(_RECENT_TRADES.items()):
        if ts < cutoff:
            _RECENT_TRADES.pop(key, None)


def _traded_recently(bot_id: int, code: str, action: str) -> bool:
    ts = _RECENT_TRADES.get((bot_id, code, action))
    return bool(ts and (time.time() - ts) < _RECENT_TRADE_TTL)


def _is_blacklist_worthy(msg: str) -> bool:
    """KIS 실패 메시지 중 '이 종목은 매매 자체가 불가' 계열만 블랙리스트 대상.

    등록 O: 매매불가·거래정지·상장폐지·정리매매·관리종목·투자경고 등 종목 자체 문제
    등록 X: 잔고 부족·rate limit·호가 오류·시간외·네트워크 (일시적·종목 탓 아님)
    """
    if not msg:
        return False
    negative = ("주문가능금액", "예수금", "잔고", "초당 거래건수",
                "호가", "시간외", "정상처리", "네트워크", "타임아웃", "token")
    for p in negative:
        if p in msg:
            return False
    positive = ("매매불가", "거래정지", "거래 정지", "상장폐지", "정리매매",
                "투자경고", "관리종목", "ETF 해지", "거래소 지정")
    for p in positive:
        if p in msg:
            return True
    return False  # 알 수 없는 실패는 오탐 방지 위해 등록 안 함


def _fetch_fill(broker: BaseBroker, order_id: str, stock_code: str,
                requested_qty: int, requested_price: float) -> OrderFill | None:
    """주문 직후 체결 평균가 조회. 시장가 주문 체결 반영 지연을 고려해 여러 번 재시도.

    안전장치: 부분 체결 행만 먼저 응답되는 KIS 지연 케이스에서 잘못된 수량으로
    기록이 덮이는 것을 막기 위해 (1) 요청 수량과 정확히 일치, (2) 체결가가
    요청가 대비 비정상(±10% 초과) 이탈이 아닐 때만 신뢰한다. 그 외엔 폴백.
    """
    if not order_id:
        return None
    price_floor = requested_price * 0.9 if requested_price > 0 else 0
    price_ceil = requested_price * 1.1 if requested_price > 0 else float("inf")
    last_fill: OrderFill | None = None
    for attempt, delay in enumerate(_FILL_LOOKUP_DELAYS):
        time.sleep(delay)
        try:
            fill = broker.get_order_fill(order_id, stock_code)
        except Exception as e:
            log.debug("체결 조회 실패 (%s, attempt %d): %s", order_id, attempt + 1, e)
            fill = None
        if not fill or fill.filled_quantity <= 0 or fill.avg_fill_price <= 0:
            continue
        last_fill = fill
        # 수량이 일치하고 가격이 정상 범위일 때만 채택
        if fill.filled_quantity == requested_qty and price_floor <= fill.avg_fill_price <= price_ceil:
            return fill
        log.debug("체결 조회 불완전 (%s): qty=%d/%d price=%.0f (요청 %.0f) → 재시도",
                  order_id, fill.filled_quantity, requested_qty,
                  fill.avg_fill_price, requested_price)
    if last_fill is not None:
        log.warning("체결 조회 불완전 → 폴백 (%s qty=%d/%d price=%.0f 요청=%.0f)",
                    order_id, last_fill.filled_quantity, requested_qty,
                    last_fill.avg_fill_price, requested_price)
    return None


def _should_place_orders(account_type: str) -> bool:
    """ENGINE_MODE + 계좌 타입에 따라 실제 주문할지 결정."""
    mode = config.engine_mode.upper()
    if mode == "DRY":
        return False
    if mode == "PAPER":
        return account_type.upper() == "MOCK"
    if mode == "LIVE":
        return True
    return False


def execute_buy_for_bot(
    client: BackendClient,
    broker: BaseBroker,
    bot: dict,
    buy_candidates: list[dict],
    cash_available: float,
) -> None:
    """BUY 시그널 후보 리스트 → 필터 → 주문 → 포지션 기록.

    buy_candidates 원소: {
        stock_code, stock_name, price, change_rate, strength, reasons
    }
    """
    bot_id = bot["id"]
    bot_name = bot.get("name", str(bot_id))
    account_type = bot.get("accountType", "MOCK")
    max_positions = bot.get("maxPositions") or 3
    trade_ratio = bot.get("tradeRatio") or 0.10

    # 현재 포지션 조회 — 중복 매수 방지, maxPositions 체크
    try:
        positions = _get_bot_positions(client, bot_id)
    except Exception as e:
        log.warning("[exec][%s] 포지션 조회 실패, 매수 skip: %s", bot_name, e)
        return

    held_codes = {p["stockCode"] for p in positions}
    open_count = len(positions)

    # 매매불가 블랙리스트 — KIS 모의/실계좌별 최근 24h 주문 실패 종목 skip
    try:
        blacklist = set(client.get_blacklist(account_type))
    except Exception as e:
        log.debug("[exec][%s] 블랙리스트 조회 실패 (skip 적용 안 됨): %s", bot_name, e)
        blacklist = set()

    if open_count >= max_positions:
        log.info("[exec][%s] 최대 보유 %d개 도달 — 신규 매수 skip", bot_name, max_positions)
        return

    slots_available = max_positions - open_count
    take_profit_rate = bot.get("takeProfitRate") or 0.08

    for cand in buy_candidates[:slots_available]:
        code = cand["stock_code"]
        if not code or code in held_codes:
            continue
        if code in blacklist:
            log.debug("[exec][%s] %s 블랙리스트 skip", bot_name, code)
            continue
        # 직전 사이클에서 같은 종목 매매한 경우 — KIS 잔고 반영 지연으로 중복될 수 있으므로 skip
        if _traded_recently(bot_id, code, "BUY") or _traded_recently(bot_id, code, "SELL"):
            log.info("[exec][%s] %s 최근 체결 TTL 내 — 중복 방지 skip", bot_name, code)
            continue

        price = float(cand["price"])
        if price <= 0:
            continue

        # 1) 예상 순수익률 필터 — 목표가 = 현재가 × (1 + takeProfitRate)
        target_price = price * (1.0 + take_profit_rate)
        is_worth, expected_net = is_profitable_target(
            price, target_price, account_type, "KOSPI", min_multiple=2.0,
        )
        if not is_worth:
            log.info("[exec][%s] %s — 예상 순수익 %.3f%% 부족 (수수료 2배 미달) skip",
                     bot_name, code, expected_net * 100)
            continue

        # 2) 투자 금액 계산
        order_budget = cash_available * trade_ratio
        if order_budget < price:
            log.info("[exec][%s] %s — 예산 %.0f원 < 단가 %.0f, skip",
                     bot_name, code, order_budget, price)
            continue

        qty = int(order_budget // price)
        total_cost, fee = buy_cost(price, qty, account_type, "KOSPI")

        log.info(
            "[exec][%s] BUY %s %s x%d @ %.0f → 비용 %.0f (수수료 %.0f) reasons=%s",
            bot_name, cand.get("stock_name", ""), code, qty, price, total_cost, fee,
            cand.get("reasons", []),
        )

        if not _should_place_orders(account_type):
            log.info("[exec][%s] DRY — 실제 주문 skip", bot_name)
            continue

        # 3) 잔고-차분 흐름:
        #    (a) 주문 직전 잔고 prev_qty 측정
        #    (b) PENDING으로 trade INSERT (멱등성 키 = clientOrderId)
        #    (c) place_buy
        #    (d) 잔고 재조회로 변화량 검증 (retry 포함)
        #    (e) 일치=FILLED 보정, 불일치=DISCREPANCY, 조회실패=PENDING 유지(자동 복구)
        client_order_id = f"BUY-{bot_id}-{code}-{uuid.uuid4().hex[:16]}"
        stock_label = f"{cand.get('stock_name') or ''} {code}".strip()

        # (a) prev_qty 측정 — broker.get_balance() 한 번. 실패 시 이번 매수 skip (다음 사이클 재시도).
        try:
            prev_balance = broker.get_balance()
        except Exception as e:
            log.warning("[exec][%s] %s 사전 잔고 조회 실패 — 매수 skip: %s",
                        bot_name, stock_label, e)
            continue
        prev_qty, _prev_avg = _get_held_qty_from_balance(prev_balance, code)

        # (b) PENDING INSERT — 추정값(요청가·요청수량)으로. 잔고 검증 후 PATCH로 보정.
        try:
            client.record_pending_trade({
                "botId": bot_id,
                "ticker": code,
                "stockName": cand.get("stock_name"),
                "action": "BUY",
                "price": price,
                "volume": qty,
                "amount": price * qty,
                "fee": fee,
                "reason": "SIGNAL",
                "signalReasons": str(cand.get("reasons", [])),
                "clientOrderId": client_order_id,
            })
        except Exception as e:
            log.warning("[exec][%s] %s PENDING INSERT 실패 — 매수 skip: %s",
                        bot_name, stock_label, e)
            continue

        # (c) 실제 주문
        try:
            result = broker.place_buy(code, qty, price=None)
            log.info("[exec][%s] %s 주문 ok=%s id=%s",
                     bot_name, stock_label, result.filled, result.order_id)
            if not result.filled:
                raw = result.raw or {}
                raw_msg = str(raw.get("msg1") or raw.get("msg") or "주문 실패")
                # FAILED 전이 + 필요시 블랙리스트
                try:
                    client.update_trade_status(client_order_id, "FAILED",
                                               orderUuid=result.order_id)
                except Exception as ex:
                    log.warning("[exec][%s] %s FAILED 마킹 실패: %s",
                                bot_name, stock_label, ex)
                if _is_blacklist_worthy(raw_msg):
                    try:
                        client.block_stock(code, account_type, raw_msg[:200], hours=6)
                        log.info("[exec][%s] %s 블랙리스트 6h (%s)",
                                 bot_name, stock_label, raw_msg[:80])
                    except Exception as ex:
                        log.warning("[exec][%s] %s 블랙리스트 등록 실패: %s",
                                    bot_name, stock_label, ex)
                else:
                    log.info("[exec][%s] %s 주문 실패(블랙 미등록): %s",
                             bot_name, stock_label, raw_msg[:80])
                continue
        except Exception as e:
            log.warning("[exec][%s] %s 주문 호출 실패 — PENDING 유지 (자동 복구): %s",
                        bot_name, stock_label, e)
            # 주문 호출 자체가 raise됐을 때 — 실제로 나갔을 수도, 안 나갔을 수도.
            # PENDING 유지 → 다음 사이클 자동 복구가 잔고로 진실 판정.
            continue

        # (d) 잔고-차분 검증 (retry 1.5s + 3.0s 총 ~5s)
        verify = _verify_balance_change(broker, code, prev_qty, expected_delta=qty)

        if verify.matched:
            # (e1) FILLED — 잔고 변화로 측정한 실제 수량·평균가로 보정
            actual_qty = verify.actual_delta
            # 가격: _fetch_fill 보조 (있으면 정확한 체결 평균가) → 없으면 잔고 평균가의 변화
            #       잔고 평균가는 누적 평균이라 신규 매수분만 정확히 분리는 어려움.
            #       _fetch_fill이 있으면 그쪽이 더 정확.
            fill = _fetch_fill(broker, result.order_id, code, qty, price)
            if fill and fill.filled_quantity == actual_qty:
                actual_price = fill.avg_fill_price
                actual_amount = fill.total_fill_amount
            else:
                # _fetch_fill 폴백 — 잔고 검증된 평균가의 가중평균으로 신규분 추정
                # prev_qty=0이면 verify.actual_avg_price가 곧 신규 체결 평균가
                if prev_qty == 0:
                    actual_price = verify.actual_avg_price
                else:
                    # 누적 평균가에서 신규분 분리 어려움 — 요청가 폴백
                    actual_price = price
                actual_amount = actual_price * actual_qty
            _, actual_fee = buy_cost(actual_price, actual_qty, account_type, "KOSPI")
            actual_cost = actual_amount + actual_fee

            log.info("[exec][%s] %s FILLED 잔고 +%d (prev=%d) avg=%.0f 비용=%.0f",
                     bot_name, stock_label, actual_qty, prev_qty, actual_price, actual_cost)

            try:
                client.upsert_position(bot_id, code, cand.get("stock_name"),
                                       actual_qty, actual_price, actual_cost)
                client.update_trade_status(
                    client_order_id, "FILLED",
                    orderUuid=result.order_id,
                    actualPrice=actual_price,
                    actualVolume=actual_qty,
                    actualAmount=actual_amount,
                    actualFee=actual_fee,
                )
                _mark_recent_trade(bot_id, code, "BUY")
            except Exception as e:
                log.warning("[exec][%s] %s FILLED 기록 실패: %s", bot_name, code, e)
            cash_available -= actual_cost

        elif verify.error == "no_change":
            # (e2) DISCREPANCY — 주문 응답은 OK였는데 잔고 변화 없음. 이상 케이스.
            log.warning("[exec][%s] %s DISCREPANCY 잔고 변화 없음 (prev=%d, expected +%d)",
                        bot_name, stock_label, prev_qty, qty)
            try:
                client.update_trade_status(client_order_id, "DISCREPANCY",
                                           orderUuid=result.order_id)
            except Exception as e:
                log.warning("[exec][%s] %s DISCREPANCY 마킹 실패: %s", bot_name, code, e)
            # 2026-05-06: 무한 재시도 차단 — DISCREPANCY 후에도 TTL 마킹 + cash 차감.
            # 실제 체결 여부 불확실하니 보수적으로 "체결됐다고 가정"하고 다음 사이클 차단.
            # 4/30 114800 7건 중복 매수 사고 (실제로 다 체결됐는데 검증 실패) 재발 방지.
            _mark_recent_trade(bot_id, code, "BUY")
            cash_available -= total_cost
        else:
            # (e3) qty_mismatch 또는 lookup_failed → DISCREPANCY 마킹 + 자동 복구 트리거
            log.warning("[exec][%s] %s 잔고 검증 불일치 (delta=%d expected=%d error=%s)",
                        bot_name, stock_label, verify.actual_delta, qty, verify.error)
            try:
                client.update_trade_status(client_order_id, "DISCREPANCY",
                                           orderUuid=result.order_id)
            except Exception as e:
                log.warning("[exec][%s] %s DISCREPANCY 마킹 실패: %s", bot_name, code, e)
            # 2026-05-06: 무한 재시도 차단 (위와 동일 사유)
            _mark_recent_trade(bot_id, code, "BUY")
            cash_available -= total_cost


def execute_exits_for_bot(
    client: BackendClient,
    broker: BaseBroker,
    bot: dict,
    flow_by_code: dict[str, dict],
) -> None:
    """보유 포지션 각각에 대해 현재가·수급을 바탕으로 exit 판단."""
    bot_id = bot["id"]
    bot_name = bot.get("name", str(bot_id))
    account_type = bot.get("accountType", "MOCK")
    investment_type = bot.get("investmentType", "MODERATE")
    risk_mode = bot.get("riskMode") or "AUTO"
    stop_loss_rate = bot.get("stopLossRate")
    take_profit_rate = bot.get("takeProfitRate")

    # 진실원: KIS 잔고. DB Position은 stale될 수 있으므로 매도 후보 판단엔 쓰지 않는다.
    # DB는 peak_price·entry_at·total_cost 등 메타데이터 병합에만 사용.
    try:
        balance = broker.get_balance()
    except Exception as e:
        log.warning("[exit][%s] KIS 잔고 조회 실패: %s", bot_name, e)
        return
    kis_positions = [p for p in balance.positions if p.quantity > 0]
    if not kis_positions:
        return
    log.info("[exit][%s] 보유 %d종 exit 판정 시작", bot_name, len(kis_positions))

    # 메타데이터용 DB 포지션 매핑
    try:
        db_positions = _get_bot_positions(client, bot_id)
    except Exception as e:
        log.debug("[exit][%s] DB 포지션 조회 실패 (메타 없이 진행): %s", bot_name, e)
        db_positions = []
    db_by_code: dict[str, dict] = {p["stockCode"]: p for p in db_positions}

    for kis_pos in kis_positions:
        code = kis_pos.stock_code
        qty = int(kis_pos.quantity)
        avg_price = float(kis_pos.avg_price)

        # 직전 사이클에서 이미 매도 시도한 종목 — KIS eventual consistency 대응
        if _traded_recently(bot_id, code, "SELL"):
            log.info("[exit][%s] %s 최근 매도 TTL 내 — skip", bot_name, code)
            continue

        # 현재가 조회
        try:
            current_price = broker.get_current_price(code)
        except Exception as e:
            log.warning("[exit][%s] %s 현재가 조회 실패: %s", bot_name, code, e)
            continue
        if current_price <= 0:
            continue

        # DB 메타데이터 병합 — 없으면 합리적 기본값
        db_meta = db_by_code.get(code) or {}
        peak_price = max(float(db_meta.get("peakPrice") or avg_price), current_price)
        entry_at = _parse_dt(db_meta.get("entryAt")) if db_meta.get("entryAt") else datetime.now()
        stock_name = kis_pos.stock_name or db_meta.get("stockName")
        initial_cost = float(db_meta.get("totalCost") or (avg_price * qty))

        flow = flow_by_code.get(code) or {}

        decision = decide_exit(
            avg_price=avg_price,
            quantity=qty,
            peak_price=peak_price,
            entry_at=entry_at,
            current_price=current_price,
            foreign_net=int(flow.get("foreign_net_qty") or 0),
            institution_net=int(flow.get("institution_net_qty") or 0),
            investment_type=investment_type,
            risk_mode=risk_mode,
            stop_loss_rate=float(stop_loss_rate) if stop_loss_rate is not None else -0.02,
            take_profit_rate=float(take_profit_rate) if take_profit_rate is not None else 0.08,
        )

        if not decision.should_sell:
            pnl_pct = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0.0
            reason = decision.reasons[0] if decision.reasons else "규칙 미트리거"
            log.info("[exit][%s] HOLD %s %s x%d avg=%.0f cur=%.0f (%+.2f%%) %s",
                     bot_name, kis_pos.stock_name or "", code, qty,
                     avg_price, current_price, pnl_pct, reason)
            continue

        stock_label = f"{stock_name or ''} {code}".strip()
        log.info("[exit][%s] SELL %s x%d @ %.0f reasons=%s urgency=%s",
                 bot_name, stock_label, qty, current_price, decision.reasons, decision.urgency)

        # 시그널 기록 (exit 근거)
        try:
            client.record_signal(bot_id, {
                "stockCode": code,
                "stockName": stock_name,
                "action": "SELL",
                "strength": 0.9 if decision.urgency == "high" else 0.6,
                "reasons": decision.reasons,
                "price": current_price,
                "executed": True,
            })
        except Exception as e:
            log.warning("[exit][%s] 시그널 기록 실패: %s", bot_name, e)

        if not _should_place_orders(account_type):
            log.info("[exit][%s] DRY — 실제 매도 skip", bot_name)
            continue

        # 잔고-차분 흐름 (BUY와 대칭, expected_delta 음수):
        client_order_id = f"SELL-{bot_id}-{code}-{uuid.uuid4().hex[:16]}"
        time.sleep(_ORDER_DELAY)

        # (a) prev_qty — exit 루프에서 받은 kis_pos 시점이지만, 매도 직전에 다시 측정해 정확도 ↑
        try:
            prev_balance = broker.get_balance()
        except Exception as e:
            log.warning("[exit][%s] %s 사전 잔고 조회 실패 — 매도 skip: %s",
                        bot_name, stock_label, e)
            continue
        prev_qty, prev_avg = _get_held_qty_from_balance(prev_balance, code)
        if prev_qty < qty:
            # 보유보다 많이 팔려는 경우 — 데이터 어긋남, 안전 skip
            log.warning("[exit][%s] %s 보유 부족 (prev_qty=%d < qty=%d) skip",
                        bot_name, stock_label, prev_qty, qty)
            continue

        # (b) PENDING INSERT — 추정값(현재가·요청수량) + reason 분류
        reason_code = _classify_exit_reason(decision.reasons)
        try:
            client.record_pending_trade({
                "botId": bot_id,
                "ticker": code,
                "stockName": stock_name,
                "action": "SELL",
                "price": current_price,
                "volume": qty,
                "amount": current_price * qty,
                "reason": reason_code,
                "signalReasons": str(decision.reasons),
                "clientOrderId": client_order_id,
            })
        except Exception as e:
            log.warning("[exit][%s] %s PENDING INSERT 실패 — 매도 skip: %s",
                        bot_name, stock_label, e)
            continue

        # (c) 실제 매도
        try:
            sell_result = broker.place_sell(code, qty, price=None)
        except Exception as e:
            log.warning("[exit][%s] %s 매도 호출 실패 — PENDING 유지 (자동 복구): %s",
                        bot_name, stock_label, e)
            continue

        if not sell_result.filled:
            raw = sell_result.raw or {}
            raw_msg = str(raw.get("msg1") or raw.get("msg") or "주문 실패")
            log.warning("[exit][%s] %s 매도 주문 실패 — FAILED 마킹: %s",
                        bot_name, stock_label, raw_msg[:120])
            try:
                client.update_trade_status(client_order_id, "FAILED",
                                           orderUuid=sell_result.order_id)
            except Exception as ex:
                log.warning("[exit][%s] %s FAILED 마킹 실패: %s", bot_name, code, ex)
            # "잔고 없음" 계열은 다음 사이클 반복 방지 차원에서 긴 TTL 마킹
            if "잔고" in raw_msg or "보유" in raw_msg:
                _mark_recent_trade(bot_id, code, "SELL")
            continue

        # (d) 잔고-차분 검증 — SELL은 expected_delta=-qty
        verify = _verify_balance_change(broker, code, prev_qty, expected_delta=-qty)

        if verify.matched:
            actual_qty = -verify.actual_delta  # 음수를 양수로
            # 체결 평균가는 잔고에서 알기 어려움 (avg_price 변화 없음) → _fetch_fill 보조
            fill = _fetch_fill(broker, sell_result.order_id, code, qty, current_price)
            if fill and fill.filled_quantity == actual_qty:
                actual_price = fill.avg_fill_price
            else:
                actual_price = current_price  # 폴백 — 매도 시점 현재가

            # 순손익 = 체결 수취(수수료+세 차감) - 원가(매수 시 누적 비용)
            # initial_cost는 DB 메타에서 (없으면 avg_price * actual_qty 폴백)
            # 부분 매도 시 actual_qty < total qty이면 비례 배분
            cost_per_unit = initial_cost / qty if qty > 0 else avg_price
            applied_initial_cost = cost_per_unit * actual_qty
            net_receive, sell_fee = sell_proceeds(actual_price, actual_qty, account_type, "KOSPI")
            net_gain = net_receive - applied_initial_cost
            profit_rate = net_gain / applied_initial_cost if applied_initial_cost > 0 else 0.0

            log.info("[exit][%s] %s FILLED 잔고 -%d (prev=%d) 체결가=%.0f net=%.0f (%.2f%%)",
                     bot_name, stock_label, actual_qty, prev_qty, actual_price,
                     net_gain, profit_rate * 100)

            try:
                client.reduce_position(bot_id, code, actual_qty)
                client.update_trade_status(
                    client_order_id, "FILLED",
                    orderUuid=sell_result.order_id,
                    actualPrice=actual_price,
                    actualVolume=actual_qty,
                    actualAmount=actual_price * actual_qty,
                    actualFee=sell_fee,
                    actualProfitRate=profit_rate,
                    actualProfitAmount=net_gain,
                )
                _mark_recent_trade(bot_id, code, "SELL")
            except Exception as e:
                log.warning("[exit][%s] %s FILLED 기록 실패: %s", bot_name, code, e)

        elif verify.error == "no_change":
            log.warning("[exit][%s] %s DISCREPANCY 잔고 변화 없음 (매도 미체결?)",
                        bot_name, stock_label)
            try:
                client.update_trade_status(client_order_id, "DISCREPANCY",
                                           orderUuid=sell_result.order_id)
            except Exception as e:
                log.warning("[exit][%s] %s DISCREPANCY 마킹 실패: %s", bot_name, code, e)
            # 2026-05-06: 무한 재시도 차단 — 보수적으로 SELL TTL 마킹.
            # 실제 체결 여부 불확실 시 다음 사이클에서 또 매도 시도하면 중복 우려.
            _mark_recent_trade(bot_id, code, "SELL")
        else:
            log.warning("[exit][%s] %s 잔고 검증 불일치 (delta=%d expected=%d error=%s)",
                        bot_name, stock_label, verify.actual_delta, -qty, verify.error)
            try:
                client.update_trade_status(client_order_id, "DISCREPANCY",
                                           orderUuid=sell_result.order_id)
            except Exception as e:
                log.warning("[exit][%s] %s DISCREPANCY 마킹 실패: %s", bot_name, code, e)
            _mark_recent_trade(bot_id, code, "SELL")


# ─── helpers ───

def _get_bot_positions(client: BackendClient, bot_id: int) -> list[dict]:
    """Backend에서 특정 봇의 보유 포지션 조회.

    Position 조회 API는 Authorization 기반이라 엔진 전용 Internal 엔드포인트 없이
    /api/positions/bots/{botId}는 memberId 매칭 필요. 엔진용으로는
    InternalController에 별도 GET 엔드포인트를 노출하지 않고
    active bot 응답과 snapshot을 조합해 쓰는 방식이 더 깔끔하지만,
    단기적으로는 그냥 /api/internal/positions를 추가하는 편이 빠름.

    임시: 스냅샷에서 추정하지 않고 Backend에 한 번 더 호출.
    """
    # 현재는 Position 조회용 internal API가 없으므로 빈 리스트 반환 → 엔진은 중복 매수 방지만 못 함.
    # TODO: Backend에 GET /api/internal/positions?botId=... 추가 후 여기서 호출.
    try:
        url = f"{client.base_url}/api/internal/positions?botId={bot_id}"
        resp = client._session.get(url, timeout=10)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        return []


def _parse_dt(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.now()


def _classify_exit_reason(reasons: list[str]) -> str:
    joined = " ".join(reasons).lower()
    if "절대 손절" in joined or "손절" in joined:
        return "STOP_LOSS"
    if "트레일링" in joined:
        return "TRAILING_STOP"
    if "익절" in joined:
        return "TAKE_PROFIT"
    return "SIGNAL"


def recover_pending_trades(client: BackendClient, broker: BaseBroker, bot: dict) -> None:
    """signal_cycle 시작 시 호출 — PENDING 상태로 잔존하는 trade를 잔고 차분으로 재검증.

    시나리오:
    - 엔진 크래시·네트워크 끊김으로 BUY/SELL 직후 status update 누락된 trade
    - 주문 호출은 raise됐지만 실제로는 KIS에 도달했을 가능성
    - eventual consistency로 첫 검증 시 잔고 미반영 → PENDING 유지된 trade

    동작:
    1. GET /api/internal/trades/pending 으로 봇의 PENDING 리스트 조회
    2. 각 PENDING마다 broker.get_balance()로 현재 보유량 측정
    3. executed_at 후 변화량을 보며 BUY/SELL 의도와 비교
    4. 5분 이상 경과해도 잔고에 안 반영됐으면 FAILED로 마킹

    한계: 같은 종목에 대해 여러 PENDING이 있으면 추적 어려움 → 가장 오래된 것 우선,
    매도 후 매수 등 순서 의존 케이스는 DISCREPANCY 마킹.
    """
    bot_id = bot["id"]
    bot_name = bot.get("name", str(bot_id))
    try:
        pending = client.get_pending_trades(bot_id)
    except Exception as e:
        log.debug("[recover][%s] PENDING 조회 실패: %s", bot_name, e)
        return
    if not pending:
        return

    log.info("[recover][%s] PENDING %d건 재검증", bot_name, len(pending))

    try:
        balance = broker.get_balance()
    except Exception as e:
        log.warning("[recover][%s] 잔고 조회 실패 — 다음 사이클로: %s", bot_name, e)
        return

    now = datetime.now()
    for trade in pending:
        try:
            cid = trade.get("clientOrderId")
            if not cid:
                continue
            ticker = trade.get("ticker")
            action = trade.get("action")
            req_qty = int(trade.get("volume") or 0)
            executed_at_str = trade.get("executedAt")
            executed_at = _parse_dt(executed_at_str) if executed_at_str else now
            elapsed = (now - executed_at).total_seconds()

            cur_qty, cur_avg = _get_held_qty_from_balance(balance, ticker)

            # PENDING 시점 잔고를 모르므로 "어떤 변화가 있어야 했는지"는 정확히 모른다.
            # 하지만 경과 시간이 충분하고(5분+) 잔고에 의도된 결과가 반영됐는지 정도는 판정 가능.
            #   BUY: cur_qty >= req_qty이면 일단 체결됐다고 간주
            #   SELL: cur_qty == 0이거나 줄어든 흔적이 있으면 체결됐다고 간주
            # 단순화 — 정확한 진실원이 부족할 때는 보수적으로 PENDING 유지
            if elapsed < _PENDING_FAIL_THRESHOLD_SEC:
                # 너무 빠른 재검증 — 다음 사이클로 미룸
                continue

            # 5분 경과 — 결정 시점
            if action == "BUY":
                # 체결 흔적: 잔고에 보유 있음
                if cur_qty > 0:
                    # 정확한 신규분 분리 어려워 PATCH 시 actualVolume·Price는 보정 안 함
                    # 자동 복구는 status 전이만 (체결가 정확도는 포기)
                    log.info("[recover][%s] BUY %s %s FILLED (잔고 %d, 경과 %.0fs)",
                             bot_name, trade.get("stockName") or "", ticker, cur_qty, elapsed)
                    client.update_trade_status(cid, "FILLED")
                else:
                    log.warning("[recover][%s] BUY %s %s FAILED (잔고 0, 경과 %.0fs)",
                                bot_name, trade.get("stockName") or "", ticker, elapsed)
                    client.update_trade_status(cid, "FAILED")
            elif action == "SELL":
                # 체결 흔적: 보유 줄어들었거나 0. PENDING 시점 보유량을 모르니 "줄어든 게 맞는지"는 모름.
                # 보수적으로 cur_qty == 0이면 FILLED, 그렇지 않으면 PENDING 유지 (요청 수량만큼 못 줄었을 수도).
                if cur_qty == 0:
                    # 2026-05-06: 사고(id=941 profit_amount NULL) 후 — recover SELL → FILLED 전이 시
                    # actualPrice/Volume/Amount/Fee를 trade의 PENDING 추정값으로라도 보내서
                    # backend 자동 매칭 로직이 profit을 계산할 수 있게 함.
                    sell_price = float(trade.get("price") or 0)
                    sell_qty = req_qty
                    if sell_price > 0 and sell_qty > 0:
                        # account_type은 PENDING 시점에 모르므로 기본 KIS 모의=MOCK으로 sell_proceeds 호출
                        # (REAL이면 backend가 자동 매칭에서 정확한 fee 재계산하면 됨)
                        try:
                            from ai.fee import sell_proceeds as _sp
                            net_receive, sell_fee = _sp(sell_price, sell_qty,
                                                        bot.get("accountType", "MOCK"), "KOSPI")
                        except Exception:
                            net_receive, sell_fee = sell_price * sell_qty, 0.0
                        log.info("[recover][%s] SELL %s %s FILLED (잔고 0, 경과 %.0fs) "
                                 "추정 체결액=%.0f fee=%.0f",
                                 bot_name, trade.get("stockName") or "", ticker, elapsed,
                                 net_receive, sell_fee)
                        client.update_trade_status(
                            cid, "FILLED",
                            actualPrice=sell_price,
                            actualVolume=sell_qty,
                            actualAmount=sell_price * sell_qty,
                            actualFee=sell_fee,
                        )
                    else:
                        log.info("[recover][%s] SELL %s %s FILLED (잔고 0, 경과 %.0fs)",
                                 bot_name, trade.get("stockName") or "", ticker, elapsed)
                        client.update_trade_status(cid, "FILLED")
                else:
                    # 보유 남아있음 — 부분체결 또는 미체결. DISCREPANCY로 사람 점검 유도.
                    log.warning("[recover][%s] SELL %s %s DISCREPANCY (잔고 %d, 요청 %d, 경과 %.0fs)",
                                bot_name, trade.get("stockName") or "", ticker, cur_qty, req_qty, elapsed)
                    client.update_trade_status(cid, "DISCREPANCY")
        except Exception as e:
            log.warning("[recover][%s] %s 처리 실패: %s",
                        bot_name, trade.get("ticker"), e)

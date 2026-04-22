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
from datetime import datetime
from typing import Any

# KIS 초당 rate limit 대응
_ORDER_DELAY = 1.0
# 주문 후 체결가 조회까지 대기 시간 — 시장가는 즉시 체결되지만 조회 반영까지 여유 필요
_FILL_LOOKUP_DELAY = 1.5
_FILL_LOOKUP_RETRY = 2

from ai.exit_signal import decide_exit
from ai.fee import buy_cost, is_profitable_target, net_pnl, sell_proceeds
from broker.base import BaseBroker, OrderFill, OrderSide
from config import config
from core.backend_client import BackendClient

log = logging.getLogger(__name__)


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
    """주문 직후 체결 평균가 조회. 시장가 주문 체결 반영 지연을 고려해 짧게 재시도.

    안전장치: 부분 체결 행만 먼저 응답되는 KIS 지연 케이스에서 잘못된 수량으로
    기록이 덮이는 것을 막기 위해 (1) 요청 수량과 정확히 일치, (2) 체결가가
    요청가 대비 비정상(±10% 초과) 이탈이 아닐 때만 신뢰한다. 그 외엔 폴백.
    """
    if not order_id:
        return None
    price_floor = requested_price * 0.9 if requested_price > 0 else 0
    price_ceil = requested_price * 1.1 if requested_price > 0 else float("inf")
    last_fill: OrderFill | None = None
    for attempt in range(_FILL_LOOKUP_RETRY):
        time.sleep(_FILL_LOOKUP_DELAY if attempt == 0 else _FILL_LOOKUP_DELAY * 2)
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

        # 3) 실제 주문 (시장가). KIS 간격은 KISBroker._api_call이 강제.
        stock_label = f"{cand.get('stock_name') or ''} {code}".strip()
        try:
            result = broker.place_buy(code, qty, price=None)
            log.info("[exec][%s] %s 주문 ok=%s id=%s",
                     bot_name, stock_label, result.filled, result.order_id)
            if not result.filled:
                raw = result.raw or {}
                raw_msg = str(raw.get("msg1") or raw.get("msg") or "주문 실패")
                # "매매불가/거래정지/상장폐지" 류만 블랙리스트. 잔고/rate limit/호가는 제외.
                if _is_blacklist_worthy(raw_msg):
                    try:
                        # 6시간만 블랙 — 일시적 차단 성격. 만료 후 자동 재시도 → 항구적이면 재등록 루프.
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
            log.warning("[exec][%s] %s 주문 실패: %s", bot_name, stock_label, e)
            continue

        # 4) 실제 체결 평균가 조회 → 요청가와 다르면 체결가로 보정.
        # 수수료는 체결금액 × 수수료율로 재계산 (MOCK=0%, REAL=0.015%)
        fill = _fetch_fill(broker, result.order_id, code, qty, price)
        if fill:
            actual_price = fill.avg_fill_price
            actual_qty = fill.filled_quantity
            actual_amount = fill.total_fill_amount
            _, actual_fee = buy_cost(actual_price, actual_qty, account_type, "KOSPI")
            actual_cost = actual_amount + actual_fee
            log.info("[exec][%s] %s 체결 확인 요청가=%.0f → 체결가=%.0f x %d "
                     "체결액=%.0f 수수료(%s)=%.0f 총비용=%.0f",
                     bot_name, stock_label, price, actual_price, actual_qty,
                     actual_amount, account_type, actual_fee, actual_cost)
        else:
            actual_price = price
            actual_qty = qty
            actual_amount = price * qty
            actual_fee = fee
            actual_cost = total_cost
            log.info("[exec][%s] %s 체결가 조회 실패 → 요청가 %.0f 기록 "
                     "수수료(%s)=%.0f 총비용=%.0f",
                     bot_name, stock_label, price, account_type, actual_fee, actual_cost)

        # 5) Backend에 포지션 + 매매이력 기록 (체결가 기준)
        try:
            client.upsert_position(bot_id, code, cand.get("stock_name"),
                                    actual_qty, actual_price, actual_cost)
            client.record_trade({
                "botId": bot_id,
                "ticker": code,
                "stockName": cand.get("stock_name"),
                "action": "BUY",
                "price": actual_price,
                "volume": actual_qty,
                "amount": actual_amount,
                "fee": actual_fee,
                "reason": "SIGNAL",
                "signalReasons": str(cand.get("reasons", [])),
            })
        except Exception as e:
            log.warning("[exec][%s] %s 기록 실패 (주문은 나감): %s", bot_name, code, e)

        # 예산 차감 (연속 매수 방지 차원)
        cash_available -= actual_cost


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

    try:
        positions = _get_bot_positions(client, bot_id)
    except Exception as e:
        log.warning("[exit][%s] 포지션 조회 실패: %s", bot_name, e)
        return

    if not positions:
        return

    for pos in positions:
        code = pos["stockCode"]

        # 현재가 조회 (가벼운 호출)
        try:
            current_price = broker.get_current_price(code)
        except Exception as e:
            log.warning("[exit][%s] %s 현재가 조회 실패: %s", bot_name, code, e)
            continue
        if current_price <= 0:
            continue

        avg_price = float(pos["avgPrice"])
        peak_price = max(float(pos["peakPrice"]), current_price)

        flow = flow_by_code.get(code) or {}
        entry_at = _parse_dt(pos.get("entryAt"))

        decision = decide_exit(
            avg_price=avg_price,
            quantity=int(pos["quantity"]),
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
            log.debug("[exit][%s] %s HOLD (%.2f%%)", bot_name, code,
                      ((current_price - avg_price) / avg_price) * 100)
            continue

        qty = int(pos["quantity"])
        stock_label = f"{pos.get('stockName') or ''} {code}".strip()
        log.info("[exit][%s] SELL %s x%d @ %.0f reasons=%s urgency=%s",
                 bot_name, stock_label, qty, current_price, decision.reasons, decision.urgency)

        # 시그널 기록 (exit 근거)
        try:
            client.record_signal(bot_id, {
                "stockCode": code,
                "stockName": pos.get("stockName"),
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

        # 실제 매도 (시장가). rate limit 회피.
        time.sleep(_ORDER_DELAY)
        try:
            sell_result = broker.place_sell(code, qty, price=None)
        except Exception as e:
            log.warning("[exit][%s] %s 매도 실패: %s", bot_name, code, e)
            continue

        # 체결 평균가 조회 → 순손익 계산은 체결가 기준
        fill = _fetch_fill(broker, sell_result.order_id, code, qty, current_price)
        if fill:
            actual_price = fill.avg_fill_price
            actual_qty = fill.filled_quantity
        else:
            actual_price = current_price
            actual_qty = qty

        # 순손익 = 체결 수취(수수료+거래세 차감) - 원가(매수 수수료 포함)
        initial_cost = float(pos.get("totalCost") or (avg_price * actual_qty))
        net_receive, sell_fee = sell_proceeds(actual_price, actual_qty, account_type, "KOSPI")
        net_gain = net_receive - initial_cost
        profit_rate = net_gain / initial_cost if initial_cost > 0 else 0.0

        if fill:
            log.info("[exit][%s] %s 체결 확인 요청가=%.0f → 체결가=%.0f x %d "
                     "수취=%.0f 수수료+세(%s)=%.0f net=%.0f (%.2f%%)",
                     bot_name, stock_label, current_price, actual_price, actual_qty,
                     net_receive, account_type, sell_fee, net_gain, profit_rate * 100)
        else:
            log.info("[exit][%s] %s 체결가 조회 실패 → 현재가 %.0f 기록 "
                     "수수료+세(%s)=%.0f net=%.0f (%.2f%%)",
                     bot_name, stock_label, current_price, account_type, sell_fee,
                     net_gain, profit_rate * 100)

        try:
            client.reduce_position(bot_id, code, actual_qty)
            client.record_trade({
                "botId": bot_id,
                "ticker": code,
                "stockName": pos.get("stockName"),
                "action": "SELL",
                "price": actual_price,
                "volume": actual_qty,
                "amount": actual_price * actual_qty,
                "fee": sell_fee,
                "profitRate": profit_rate,
                "profitAmount": net_gain,
                "reason": _classify_exit_reason(decision.reasons),
                "signalReasons": str(decision.reasons),
            })
        except Exception as e:
            log.warning("[exit][%s] %s 기록 실패: %s", bot_name, code, e)


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

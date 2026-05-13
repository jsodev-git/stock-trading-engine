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
from datetime import date
from typing import Any

from ai.signal import score_buy_candidate
from broker import get_broker
from broker.base import BaseBroker
from core.backend_client import BackendClient
from core.executor import execute_buy_for_bot, execute_exits_for_bot, recover_pending_trades

# KIS는 초당 API 요청 수가 제한돼 있어 각 호출 사이 지연
_KIS_CALL_DELAY = 1.0

log = logging.getLogger(__name__)

# 단일 BUY threshold — 분석 결과 시그널 강도와 수익률이 역상관이라 투자성향별 분리 무의미.
# 성향 차이는 tradeRatio·maxPositions·stop_loss/take_profit로 반영.
_BUY_THRESHOLD = 0.40

# daily_closes 당일 캐시 — 장중에는 어제까지의 종가만 SMA/RSI에 사용하므로
# 하루에 한 번만 조회해도 충분. 매 사이클 재조회는 KIS rate limit + sleep으로
# 사이클 당 ~13s 낭비 (10종 × 1.3s)였음.
_daily_closes_cache: dict[str, list[float]] = {}
_daily_closes_cache_date: date | None = None


def _get_daily_closes_cached(broker: BaseBroker, code: str, days: int = 30) -> list[float]:
    """당일 캐시. 날짜 바뀌면 자동 flush (다음 장 열릴 때 새 종가 포함 재조회)."""
    global _daily_closes_cache_date
    today = date.today()
    if _daily_closes_cache_date != today:
        _daily_closes_cache.clear()
        _daily_closes_cache_date = today
    if code in _daily_closes_cache:
        return _daily_closes_cache[code]
    closes = broker.get_daily_closes(code, days=days) or []
    if closes:
        _daily_closes_cache[code] = closes
    return closes


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = (sum((x - ma) ** 2 for x in a)) ** 0.5
    db = (sum((x - mb) ** 2 for x in b)) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _detect_hot_theme(closes_by_code: dict[str, list[float]]) -> set[str]:
    """당일 강세 테마 멤버 집합 반환.

    각 종목의 최근 5일 일간 수익률을 계산하고, 서로의 평균 상관이 높은 종목들이
    3개 이상이면 "테마"로 간주. 상관관계로 동반 상승 패턴 탐지.
    """
    returns_by: dict[str, list[float]] = {}
    for code, closes in closes_by_code.items():
        if len(closes) < 6:
            continue
        rets: list[float] = []
        for i in range(len(closes) - 5, len(closes)):
            if i > 0 and closes[i - 1] > 0:
                rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
        if len(rets) >= 4:
            returns_by[code] = rets
    if len(returns_by) < 3:
        return set()

    codes = list(returns_by)
    avg_corr: dict[str, float] = {}
    for c1 in codes:
        corrs = [_pearson(returns_by[c1], returns_by[c2]) for c2 in codes if c2 != c1]
        avg_corr[c1] = sum(corrs) / len(corrs) if corrs else 0.0

    # 평균 상관 0.4 이상을 테마 멤버로
    members = {c for c, v in avg_corr.items() if v >= 0.4}
    return members if len(members) >= 3 else set()


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. 데이터 부족 시 None."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _is_overbought(closes: list[float], current_price: float) -> tuple[bool, str]:
    """과매수 판정 — 백스탑: SMA5 +8% 초과 OR RSI(14) > 75.

    Why: pool을 등락률 -1~+5%로 좁혔으므로 필터는 안전장치 역할만.
    실거래에서 급등 추격 손실을 막는 최후 방어선이라 완전 제거는 하지 않음.
    """
    if len(closes) < 5:
        return False, ""
    sma5 = sum(closes[-5:]) / 5
    if sma5 > 0 and current_price > sma5 * 1.08:
        return True, f"5일 SMA({sma5:.0f}) 대비 현재가 +{((current_price / sma5) - 1) * 100:.1f}% 과열"
    rsi = _compute_rsi(closes)
    if rsi is not None and rsi > 75:
        return True, f"RSI {rsi:.1f} 과매수 (>75)"
    return False, ""


# 2026-05-06: ETF/인버스/레버리지 종목 식별. 종목명 패턴 기반.
# 데이터 검증: ETF 그룹 18건 매매 net -136k vs 일반 12건 +89k.
# 모멘텀 시그널(거래량+등락률+수급)은 인버스/레버리지 ETF 특성과 부적합.
_ETF_NAME_PATTERNS = (
    "KODEX", "TIGER", "ARIRANG", "HANARO", "KBSTAR", "KOSEF",
    "ACE", "PLUS", "RISE", "SOL", "WOORI",
    # 동일 종류라도 키워드만으로 잡힘 (예: "삼성 인버스 2X WTI원유 ETN")
    "인버스", "레버리지", "선물", "ETN",
)


def _is_etf(scan_row: dict) -> bool:
    """종목명 키워드로 ETF/ETN/인버스/레버리지 식별.

    KIS rank 응답의 stock_name 또는 ticker 첫 글자(Q는 ETN)로 판정.
    완벽하진 않지만 거래량 상위에 자주 잡히는 종목들은 모두 잡힘.
    """
    name = (scan_row.get("stock_name") or "").upper()
    code = scan_row.get("stock_code") or ""
    # ticker가 알파벳으로 시작하면 ETN (예: Q530036)
    if code and not code[0].isdigit():
        return True
    for pattern in _ETF_NAME_PATTERNS:
        if pattern.upper() in name:
            return True
    return False


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

    # 0. KOSPI 약세장 체크 (2026-05-13) — 시장 자체가 -1% 이하 약세이면 BUY threshold 상향.
    # 데이터 근거: 5/12 시장 급락 시 6분 만에 -4% 손절 5건 폭주 (-284k). 시장 환경 회피.
    try:
        kospi_change = broker.get_kospi_change_rate()
    except Exception as e:
        log.debug("[signal_job] KOSPI 등락률 조회 실패: %s", e)
        kospi_change = None
    bearish_market = kospi_change is not None and kospi_change <= -0.01
    if kospi_change is not None:
        log.info("[signal_job] KOSPI 등락률 %+.2f%% (약세장 %s)",
                 kospi_change * 100, "ON" if bearish_market else "off")

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

        # 2. 후보 종목 — 모멘텀 초입만: 거래량 상위 중 등락률 -1~+5% 범위
        # Why: 기존 `거래량 ∩ 등락률 상위`는 +20~30% 급등 종목만 모여 과매수 필터에
        # 100% 걸리는 구조적 모순. pool 자체를 "건강한 상승" 대역으로 교체.
        volume_by_code: dict[str, dict] = {r["stock_code"]: r for r in volume_rows}
        price_by_code: dict[str, dict] = {r["stock_code"]: r for r in price_rows}

        def _cr(code: str) -> float:
            return float(
                (volume_by_code.get(code) or {}).get("change_rate")
                or (price_by_code.get(code) or {}).get("change_rate")
                or 0
            )

        # change_rate는 소수 단위(0.03 = 3%). -1~+8% 범위.
        # 상한 +8%는 SMA5 백스탑(+8%)과 정합. 5~8% 건강 모멘텀 종목이 후보에 진입 가능.
        # 2026-05-06: ETF/인버스/레버리지 종목 hard exclude.
        # 데이터 분석(4/27~5/4) 결과 ETF 그룹 net -136k vs 일반 +89k. 모멘텀 시그널과
        # 인버스 ETF는 본질적 부적합 (시장 약세에 인버스 상승 → 우리 시그널 매수 → 시장 회복 시 손실).
        hot_codes = [
            c for c in volume_by_code
            if -0.01 <= _cr(c) <= 0.08 and not _is_etf(volume_by_code[c])
        ]
        # 거래량 순위대로 정렬 (등락률 순위 아님 — pool 편향 제거)
        hot_codes.sort(key=lambda c: volume_by_code[c].get("rank", 9999))
        hot_codes = hot_codes[:10]  # KIS rate limit 고려

        log.info("[signal_job] 핫 종목 %d개 분석 (등락률 -1~+8%% 필터, ETF 제외)", len(hot_codes))

        time.sleep(_KIS_CALL_DELAY)  # price_rankers → flow 사이 지연

        # 수급 수집 대상 = hot_codes + 봇 보유 종목 (exit 판정에 필요)
        # 봇별 held set도 저장 — 봇 루프에서 중복 BUY 시그널 차단에 사용
        held_codes: set[str] = set()
        held_by_bot: dict[int, set[str]] = {}
        for bot in kr_bots:
            try:
                positions = client.get_positions(bot["id"])
                codes = {p.get("stockCode") for p in positions if p.get("stockCode")}
                held_by_bot[bot["id"]] = codes
                held_codes.update(codes)
            except Exception as e:
                log.debug("[signal_job] 봇 %s 포지션 조회 실패: %s", bot["id"], e)
                held_by_bot[bot["id"]] = set()
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

        # 2-1. 핫 종목 일봉 수집 — SMA 시그널용 + 테마 감지용 + 과매수 하드 필터용
        closes_by_code: dict[str, list[float]] = {}
        overbought_codes: set[str] = set()
        overbought_reason: dict[str, str] = {}
        for code in hot_codes:
            try:
                cached = code in _daily_closes_cache
                closes = _get_daily_closes_cached(broker, code, days=30)
                if closes:
                    closes_by_code[code] = closes
                cur = price_by_code.get(code, {}).get("price") or (closes[-1] if closes else 0)
                if closes and cur:
                    over, reason = _is_overbought(closes, float(cur))
                    if over:
                        overbought_codes.add(code)
                        overbought_reason[code] = reason
                if not cached:
                    time.sleep(0.3)  # KIS rate limit — 캐시 hit 때는 sleep 불필요
            except Exception as e:
                log.debug("[signal_job] %s 일봉 조회 실패(무시): %s", code, e)
        if overbought_codes:
            log.info("[signal_job] 과매수 필터: %d건 제외 (%s)",
                     len(overbought_codes),
                     ", ".join(f"{c}:{overbought_reason[c][:40]}" for c in list(overbought_codes)[:3]))

        # 2-2. 테마 감지 — 5일 수익률 상관관계 높은 종목군
        theme_members = _detect_hot_theme(closes_by_code)
        if theme_members:
            log.info("[signal_job] 오늘 강세 테마 %d종: %s",
                     len(theme_members), sorted(theme_members))

        # 3. 봇별 시그널 생성 + 실행
        for bot in kr_bots:
            # 약세장(-1% 이하)이면 BUY threshold +0.15 상향 — 보수적 진입.
            threshold = _BUY_THRESHOLD + (0.15 if bearish_market else 0.0)
            bot_id = bot["id"]
            if bearish_market:
                log.info("[signal_job][%s] KOSPI 약세장 — threshold %.2f → %.2f",
                         bot.get("name"), _BUY_THRESHOLD, threshold)

            # 당일 손절 종목 재매수 금지 — 2시간 내 매도된 티커 (6h는 너무 보수적)
            try:
                recent_sold = set(client.get_recent_sold_tickers(bot_id, hours=2))
            except Exception as e:
                log.debug("[signal_job][%s] 최근 매도 조회 실패(무시): %s", bot.get("name"), e)
                recent_sold = set()
            if recent_sold:
                log.info("[signal_job][%s] 최근 매도 %d종 재매수 금지: %s",
                         bot.get("name"), len(recent_sold), sorted(recent_sold))

            # 학습 루프 (2026-05-06) — 직전 30일 종목별 누적 net_pnl 조회.
            # 시그널 score_buy_candidate에 historical_pnl 전달해 저성과 감점.
            try:
                ticker_pnl = client.get_ticker_pnl(bot_id, days=30)
            except Exception as e:
                log.debug("[signal_job][%s] ticker_pnl 조회 실패(무시): %s", bot.get("name"), e)
                ticker_pnl = {}
            if ticker_pnl:
                neg = {t: v for t, v in ticker_pnl.items() if v < 0}
                if neg:
                    log.info("[signal_job][%s] 누적 손실 종목 %d종 감점 적용: %s",
                             bot.get("name"), len(neg),
                             ", ".join(f"{t}({int(v):+,})" for t, v in
                                       sorted(neg.items(), key=lambda x: x[1])[:5]))

            generated = 0
            buy_candidates: list[dict[str, Any]] = []
            bot_held = held_by_bot.get(bot_id, set())
            for code in hot_codes:
                # BUY 차단 이유 — 시그널 기록은 하되 BUY 후보에서만 제외
                blocked_reason = None
                if code in bot_held:
                    blocked_reason = "이미 보유 중 (중복 매수 방지)"
                elif code in overbought_codes:
                    blocked_reason = f"과매수 skip: {overbought_reason.get(code, '')}"
                elif code in recent_sold:
                    blocked_reason = "당일 매도 종목 재매수 금지"
                v = volume_by_code.get(code) or {}
                p = price_by_code.get(code) or {}
                f = flow_by_code.get(code) or {
                    "foreign_net_qty": 0, "institution_net_qty": 0,
                    "individual_net_qty": 0,
                }
                _cr_val = p.get("change_rate") or v.get("change_rate") or 0
                signal = score_buy_candidate(
                    stock_code=code,
                    stock_name=v.get("stock_name") or p.get("stock_name") or "",
                    price=p.get("price") or v.get("price") or 0,
                    change_rate=_cr_val,
                    volume_rank=v.get("rank"),
                    price_rank=p.get("rank"),
                    foreign_net=f.get("foreign_net_qty", 0),
                    institution_net=f.get("institution_net_qty", 0),
                    individual_net=f.get("individual_net_qty", 0),
                    ranker_size=top_n,
                    daily_closes=closes_by_code.get(code),
                    in_hot_theme=(code in theme_members),
                    historical_pnl=ticker_pnl.get(code),
                )
                action = "BUY" if (signal.strength >= threshold and blocked_reason is None) else "HOLD"
                signal_reasons = list(signal.reasons)
                if blocked_reason and signal.strength >= threshold:
                    signal_reasons.append(f"BLOCKED: {blocked_reason}")
                log.info("[signal_job][%s] %s %s strength=%.2f cr=%+.2f%% %s reasons=%s",
                         bot.get("name"), action, code,
                         signal.strength, (_cr_val * 100 if _cr_val else 0.0),
                         f"BLOCKED({blocked_reason})" if blocked_reason else "",
                         list(signal.reasons)[:4])
                payload: dict[str, Any] = {
                    "stockCode": signal.stock_code,
                    "stockName": signal.stock_name,
                    "action": action,
                    "strength": signal.strength,
                    "reasons": signal_reasons,
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
                        "reasons": signal_reasons,
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
        # 0. 자동 복구 — 이전 사이클에서 PENDING 잔존 trade 잔고로 재검증
        recover_pending_trades(client, bot_broker, bot)

        # 1. Exit 먼저 — 새 진입 전 청산부터
        execute_exits_for_bot(client, bot_broker, bot, flow_by_code)

        # 2. 진입 — 실제 주문가능금액 확인 후. Balance.cash는 매도 체결 직후
        #    locked로 묶여 음수로 떨어질 수 있으므로 KIS 주문가능금액 API 사용.
        orderable_cash = 0.0
        try:
            orderable_cash = bot_broker.get_orderable_cash()
        except Exception as e:
            log.warning("[signal_job][%s] 주문가능금액 조회 실패, BUY skip: %s", bot_name, e)

        if orderable_cash > 0 and buy_candidates:
            log.info("[signal_job][%s] 주문가능금액 %.0f원, BUY 후보 %d건",
                     bot_name, orderable_cash, len(buy_candidates))
            execute_buy_for_bot(client, bot_broker, bot, buy_candidates, orderable_cash)
        elif orderable_cash <= 0:
            log.info("[signal_job][%s] 주문가능금액 0원, BUY skip", bot_name)
    finally:
        try:
            bot_broker.disconnect()
        except Exception:
            pass

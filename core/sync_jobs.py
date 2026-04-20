"""엔진이 주기적으로 수행하는 동기화 작업들.

- sync_balances: 실행 중인 봇의 잔고·보유종목을 브로커에서 조회해 Backend에 스냅샷 기록
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from broker import get_broker
from core.backend_client import BackendClient
from core.market_session import is_market_open

log = logging.getLogger(__name__)


def sync_balances(client: BackendClient) -> None:
    """실행 중 봇들의 최신 잔고를 브로커에서 당겨 Backend로 전송.

    장 마감 시에도 호출은 하되, 시세가 전일 종가 기준이므로 여전히 의미 있는 스냅샷이 된다.
    단 KIS 토큰은 계정당 공유되므로 같은 계좌의 다른 봇에 토큰을 재활용하는 건 Phase-later 최적화.
    """
    try:
        bots = client.get_active_bots()
    except Exception as e:
        log.warning("[sync_balances] 봇 목록 조회 실패: %s", e)
        return

    log.info("[sync_balances] %d개 봇 처리", len(bots))

    for bot in bots:
        bot_id = bot["id"]
        bot_name = bot.get("name", str(bot_id))
        broker_name = bot.get("broker", "KIWOOM")
        market = bot.get("market", "KR")
        account_type = bot.get("accountType", "MOCK")
        credentials = bot.get("credentials") or {}

        broker = get_broker(broker_name, market=market, account_type=account_type)
        try:
            broker.connect(credentials)
            balance = broker.get_balance()
        except Exception as e:
            log.warning("[sync_balances][%s] 연결/조회 실패: %s", bot_name, e)
            try:
                broker.disconnect()
            except Exception:  # pragma: no cover
                pass
            continue

        payload = {
            "botId": bot_id,
            "cash": balance.cash,
            "locked": balance.locked,
            "totalEval": balance.total_eval,
            "currency": balance.currency,
            "positions": [asdict(p) for p in balance.positions],
        }
        # Position dataclass는 stockCode/stockName/quantity/avgPrice/currentPrice 필드를 가짐
        # asdict 결과 key는 snake_case가 아닌 속성명 그대로 나오지만 base.py는 이미 camelCase로 정의.
        try:
            client.record_account_snapshot(payload)
            log.info(
                "[sync_balances][%s] 스냅샷 저장: cash=%.0f eval=%.0f positions=%d (%s)",
                bot_name, balance.cash, balance.total_eval, len(balance.positions),
                "장중" if is_market_open(market) else "장마감",
            )
        except Exception as e:
            log.warning("[sync_balances][%s] 스냅샷 전송 실패: %s", bot_name, e)

        # 포지션 최신 시세 일괄 갱신 (Position.lastPrice + peakPrice)
        try:
            price_entries = [
                {"stockCode": p.stock_code, "currentPrice": p.current_price}
                for p in balance.positions if p.current_price > 0
            ]
            client.update_position_prices(bot_id, price_entries)
        except Exception as e:
            log.warning("[sync_balances][%s] 포지션 가격 갱신 실패: %s", bot_name, e)
        finally:
            try:
                broker.disconnect()
            except Exception:  # pragma: no cover
                pass

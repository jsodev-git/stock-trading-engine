"""엔진 엔트리 포인트.

실행 흐름:
1. Backend에서 실행 중인 봇 목록 폴링
2. 각 봇별로 시장 오픈 여부 확인
3. 오픈 상태면 broker factory로 적절한 브로커 생성 → 연결 → 1 사이클 수행
4. 매매 결과는 Backend `/internal/trades`로 전송

현재는 루프 골격만 있으며 실제 스크리닝·매매는 각 모듈에서 단계적으로 채운다.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from broker import get_broker
from config import config
from core.backend_client import BackendClient
from core.market_session import is_market_open

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("engine")


def run_cycle(client: BackendClient) -> None:
    bots = client.get_active_bots()
    log.info("실행 중 봇 %d개 조회", len(bots))

    for bot in bots:
        market = bot.get("market", "KR")
        if not is_market_open(market):
            log.debug("[%s] %s 장 마감 상태 — skip", bot.get("name"), market)
            continue

        broker_name = bot.get("broker", "KIWOOM")
        account_type = bot.get("accountType", "MOCK")
        credentials = bot.get("credentials") or {}

        log.info("[%s] 사이클 진행 broker=%s market=%s account=%s",
                 bot.get("name"), broker_name, market, account_type)

        broker = get_broker(broker_name, market=market, account_type=account_type)
        try:
            broker.connect(credentials)
            # TODO: 스크리닝 → 시그널 → 주문 → 기록
            _ = broker.get_balance()
        finally:
            broker.disconnect()


def main() -> None:
    log.info("엔진 시작 mode=%s market=%s", config.engine_mode, config.market)
    client = BackendClient()

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    scheduler.add_job(lambda: run_cycle(client), "interval", minutes=5, next_run_time=None)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("엔진 종료")


if __name__ == "__main__":
    main()

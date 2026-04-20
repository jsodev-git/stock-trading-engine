"""엔진 엔트리 포인트.

스케줄된 작업:
- balance sync (60s)  — 실행 중 봇의 잔고·보유종목 스냅샷을 Backend에 기록
- trading cycle (5min) — 장 오픈 시 스크리닝 → 시그널 → 주문 (단계적 구현)

엔진 ↔ Backend 역할:
- Backend는 봇 설정/인증정보·DB 저장소를 제공
- 엔진은 브로커 API 호출·매매 판단·결과 기록 담당
- 분석용 데이터(스냅샷·매매이력·시그널 로그)는 모두 DB에 남긴다
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from broker import get_broker
from config import config
from core.backend_client import BackendClient
from core.market_session import is_market_open
from core.sync_jobs import sync_balances

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("engine")


def run_trading_cycle(client: BackendClient) -> None:
    """장 시간일 때만 실행되는 매매 사이클 (스크리닝 → 시그널 → 주문)."""
    try:
        bots = client.get_active_bots()
    except Exception as e:
        log.warning("[trading] 봇 목록 조회 실패: %s", e)
        return

    for bot in bots:
        market = bot.get("market", "KR")
        if not is_market_open(market):
            continue

        broker_name = bot.get("broker", "KIWOOM")
        account_type = bot.get("accountType", "MOCK")
        credentials = bot.get("credentials") or {}

        log.info("[trading][%s] 사이클 진행 broker=%s market=%s account=%s",
                 bot.get("name"), broker_name, market, account_type)

        broker = get_broker(broker_name, market=market, account_type=account_type)
        try:
            broker.connect(credentials)
            # TODO: 스크리닝(strategy) → 시그널(ai) → 주문 → 매매이력 기록
            _ = broker.get_balance()
        finally:
            broker.disconnect()


def main() -> None:
    log.info("엔진 시작 mode=%s market=%s", config.engine_mode, config.market)
    client = BackendClient()

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    # 잔고 실시간 반영 — 1분 주기
    scheduler.add_job(lambda: sync_balances(client), "interval", seconds=60,
                      id="sync_balances", next_run_time=None, coalesce=True, max_instances=1)
    # 매매 사이클 — 5분 주기
    scheduler.add_job(lambda: run_trading_cycle(client), "interval", minutes=5,
                      id="trading_cycle", next_run_time=None, coalesce=True, max_instances=1)

    log.info("스케줄러 시작: sync_balances=60s, trading_cycle=300s")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("엔진 종료")


if __name__ == "__main__":
    main()

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
import logging.handlers
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from broker import get_broker
from config import config
from core.backend_client import BackendClient
from core.market_session import is_market_open
from core.signal_jobs import scan_and_signal_kr
from core.sync_jobs import sync_balances

LOG_FILE = Path(__file__).resolve().parent / "engine.log"
_FORMATTER = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# 루트 로거에 stdout + rotating file handler 부착
_root = logging.getLogger()
_root.setLevel(logging.INFO)
if not _root.handlers:
    _stream = logging.StreamHandler()
    _stream.setFormatter(_FORMATTER)
    _root.addHandler(_stream)

    _file = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    _file.setFormatter(_FORMATTER)
    _root.addHandler(_file)

# APScheduler의 도배성 "Running job..." 정보는 warning 이상만 출력
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

log = logging.getLogger("engine")


def run_signal_cycle(client: BackendClient) -> None:
    """장 시간일 때 스크리닝 + 시그널 생성 (DRY 모드에서는 저장만, 주문 실행 없음)."""
    if not is_market_open("KR"):
        log.debug("[signal] KR 장 마감 — skip")
        return
    scan_and_signal_kr(client, top_n=30)


def main() -> None:
    log.info("엔진 시작 mode=%s market=%s", config.engine_mode, config.market)
    client = BackendClient()

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    now = datetime.now()
    # 잔고 실시간 반영 — 즉시 1회 + 이후 60초 주기
    scheduler.add_job(lambda: sync_balances(client), "interval", seconds=60,
                      id="sync_balances", next_run_time=now,
                      coalesce=True, max_instances=1)
    # 시그널 사이클 — 즉시 1회 + 이후 90초 주기 (KIS rate limit 고려 sync와 엇갈림)
    from datetime import timedelta
    scheduler.add_job(lambda: run_signal_cycle(client), "interval", seconds=90,
                      id="signal_cycle", next_run_time=now + timedelta(seconds=15),
                      coalesce=True, max_instances=1)

    log.info("스케줄러 시작: sync_balances=60s, signal_cycle=90s (첫 실행 즉시)")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("엔진 종료")


if __name__ == "__main__":
    main()

# Stock Trading Engine (Python, Windows)

키움 OpenAPI+ 기반 자동매매 엔진. 키움이 COM 기반이라 **Windows에서만** 실행됩니다.

## 사전 준비
- Windows 10/11 + Python 3.11+
- 키움증권 실계좌 또는 모의투자 계좌
- [키움 OpenAPI+ 모듈](https://www3.kiwoom.com/) 설치 + 최초 1회 버전 업데이트
- PostgreSQL 18 (Backend와 동일한 DB)

## 설치
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# .env 편집 후
python main.py
```

## 구조
```
trading/
├── main.py              # 엔트리 — APScheduler로 주기 실행
├── config.py            # 환경변수 로더
├── broker/
│   ├── base.py          # BaseBroker 추상 인터페이스
│   └── kiwoom.py        # 키움 OpenAPI+ 구현 (스텁)
├── strategy/
│   ├── kr/theme_scanner.py   # 국내 테마 스크리너
│   └── us/sector_scanner.py  # 미국 섹터 스크리너 (추후)
├── ai/
│   ├── news.py          # 뉴스 감성 분석
│   ├── theme.py         # 테마 분류
│   └── signal.py        # 매매 시그널
├── core/
│   ├── backend_client.py  # Backend REST 클라이언트
│   └── market_session.py  # 장 시간 판단
└── tests/
```

## 테스트
```bash
pytest tests/
```

## 운영 원칙
- **분석·통계용 데이터**는 엔진이 DB에 직접 기록
- **실시간 명령/조회**는 Backend REST API 사용
- `ENGINE_MODE=DRY`: 실제 주문 미실행 (디버깅용)
- `ENGINE_MODE=PAPER`: 모의투자 계좌만 대상
- `ENGINE_MODE=LIVE`: 실거래 계좌 포함

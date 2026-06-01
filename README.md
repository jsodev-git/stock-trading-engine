# Stock Auto Trading — Trading Engine

국내(KRX) 주식 **AI 기반 자동매매 플랫폼**의 매매 엔진입니다.
다중 브로커 추상화 위에 시그널 생성 / 주문 실행 / 잔고·체결 동기화 / 청산 규칙을 담당합니다.

> 본 프로젝트는 3개 레포로 구성됩니다.
> - Backend — Spring Boot API 서버
> - Frontend — Next.js 14 (PWA) 웹 클라이언트
> - **Trading Engine (현재 레포)** — Python 매매 엔진

---

## 현재 상태

- **검증된 브로커**: 키움증권 (실거래 · 모의투자 동작 확인)
- **구조만 마련된 브로커**: 한국투자증권 (KIS) — REST 클라이언트 / 토큰 캐시 / rate limit 처리는 구현, **실매매 검증 미완**.
- **국내(KRX)** 만 지원. 미국 시장(`strategy/us/`) 은 디렉토리만 존재.
- 시그널 점수화 / AUTO 청산 7규칙 / 투자성향별 파라미터 차등 적용.
- 실거래 신뢰성 재설계 진행 중 (KIS 진실 원천 / 멱등성 / 상태 머신 / 후검증).

## 플랫폼 제약

- **키움 OpenAPI+ 는 Windows COM** 기반이라 키움 사용 시 **Windows 필수**.
- KIS 만 사용한다면 OS 제약 없음 (단, 실매매 검증 미완).
- Python **3.11+**

## Tech Stack

- **Python 3.11+**
- `pykiwoom`, `PyQt5` (키움 COM 인터페이스)
- `requests` (KIS REST API)
- `pandas`, `ta` (지표)
- `APScheduler` (주기 실행)

## 디렉토리 구조

```
trading/
├── main.py                    # 엔트리 (APScheduler 기반 주기 실행)
├── config.py                  # 환경변수 로더
├── broker/
│   ├── base.py                # BaseBroker 추상 인터페이스 + BrokerName enum
│   ├── factory.py             # get_broker(name, market, account_type)
│   ├── kiwoom.py              # 키움 OpenAPI+ 구현 (검증됨)
│   └── kis.py                 # KIS REST 구현 (구조 / 토큰 캐시 / rate limit)
├── strategy/
│   ├── kr/                    # 국내 테마 스크리너
│   └── us/                    # 미국 섹터 스크리너 (미구현)
├── ai/
│   ├── news.py                # 뉴스 감성 분석
│   ├── theme.py               # 테마 분류
│   ├── signal.py              # 매매 시그널 점수화
│   ├── exit_signal.py         # AUTO 청산 규칙
│   └── fee.py                 # 수수료·세 계산 (net 수익률용)
├── core/
│   ├── backend_client.py      # Backend REST 클라이언트 (JWT)
│   ├── market_session.py      # 장 시간 판단 (KR/US)
│   ├── executor.py            # 주문 실행 (멱등성 키 + 후검증)
│   ├── signal_jobs.py         # 시그널 생성 잡
│   └── sync_jobs.py           # 잔고 · 체결 동기화 잡
└── tests/
```

## 운영 원칙

- **데이터 분리**
  - 실시간 명령 / 조회 → Backend REST API
  - 분석 · 통계 데이터 → 엔진이 DB 에 직접 적재 (signal / snapshot / trade)
- **모드 분리** (`ENGINE_MODE`)
  - `DRY` — 실제 주문 미실행 (디버깅)
  - `PAPER` — 모의투자 계좌만 대상
  - `LIVE` — 실거래 계좌 포함
- **net 수익 기준**: 단타 왕복 약 0.26% 비용을 시그널·청산에 반영.
- **KIS rate limit 처리**: 토큰 발급 1/분, 일반 호출 1/초 → 디스크 캐시 + 토큰 재사용.

## 매매 로직 (요약)

- **시그널 점수화**: 테마 / 수급 / 뉴스 / 기술적 지표를 가중 합산.
- **AUTO 청산 7규칙**: 손절 · 익절 · 시간 · 변동성 · 신호 소멸 · 시장 / 종목 이상 등.
- **투자성향별 파라미터**: AGGRESSIVE / MODERATE / CONSERVATIVE 가 손절폭 · 익절폭 · 포지션 사이즈 등을 차등.

## 실행

### 사전 준비
- Windows 10 / 11 (키움 사용 시)
- Python 3.11+
- 키움증권 실계좌 또는 모의투자 계좌
- 키움 OpenAPI+ 모듈 설치 + 최초 1회 버전 업데이트
- Backend 가 떠 있고 동일 PostgreSQL 18 에 접근 가능해야 함

### 설치 / 실행
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env       # .env 편집
python main.py
```

### 테스트
```bash
pytest tests/
```

### 환경 변수 (`.env`)
```
ENGINE_MODE=DRY              # DRY / PAPER / LIVE
BACKEND_BASE_URL=http://localhost:8080
ENGINE_API_TOKEN=...         # /internal/* API 인증
DB_URL=postgresql://...
KIS_BASE_URL=...             # KIS REST
```

## 아키텍처

```
Frontend (Next.js PWA)
        │  REST + JWT
        ▼
Backend (Spring Boot)  ◄──── REST ──── Trading Engine (현재 레포)
        │                                       │
        └──────────► PostgreSQL ◄───────────────┘
                  (엔진은 분석·통계는 직접 기록)
```

## 새 브로커 추가 (Engine)

1. `broker/xxx.py` 에 `BaseBroker` 구현 (`connect` / `get_balance` / `place_order` / ...)
2. `broker/factory.py` 의 `get_broker()` 에 등록
3. Backend `Broker` enum + Frontend `BROKER_CREDENTIAL_FIELDS` 에도 추가

## 로드맵

- [x] 키움 실매매 PoC
- [x] KIS REST / 토큰 캐시 / rate limit 구조
- [ ] 실거래 신뢰성 재설계 (진행 중)
- [ ] KIS 실매매 검증
- [ ] 미국 시장 (KIS Overseas) 확장

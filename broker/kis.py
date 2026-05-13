"""한국투자증권 (KIS Developers) 브로커 구현.

KIS는 REST API 기반이라 OS 제약이 없다. AppKey/AppSecret로 OAuth 토큰 발급 후
해당 토큰으로 시세·주문 API를 호출한다.

- credentials 스키마: {"appKey": "...", "appSecret": "...", "accountNo": "..."}
- accountNo 포맷: "12345678-01" (계좌번호 8자리 - 상품코드 2자리) 또는 붙여서 "1234567801"
  내부에서 CANO / ACNT_PRDT_CD로 분리.
- account_type REAL  → 실계좌 엔드포인트
- account_type MOCK  → 모의투자 엔드포인트 (서로 다른 도메인·tr_id 접미사)

공식 문서: https://apiportal.koreainvestment.com/
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from .base import (
    AccountType,
    BaseBroker,
    Balance,
    BrokerName,
    Market,
    OrderFill,
    OrderResult,
    OrderSide,
    Position,
)

log = logging.getLogger(__name__)


@dataclass
class _Token:
    value: str
    expires_at: float


class KISBroker(BaseBroker):
    """한국투자증권 KIS Developers REST 연동.

    KIS는 토큰 발급을 1분 1회로 제한하므로, 같은 (appKey, account_type) 조합은
    프로세스 전역에서 토큰을 공유한다. 그렇지 않으면 sync_balances와 trading_cycle이
    동시에 실행될 때 한쪽이 403을 받는다.
    """

    name = BrokerName.KIS

    BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
    BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"

    TIMEOUT = 10  # seconds

    # (app_key, account_type) → Token. 클래스 레벨로 공유
    _TOKEN_CACHE: dict[tuple[str, str], _Token] = {}
    _TOKEN_LOCK = threading.Lock()
    # 프로세스 재시작 시 rate limit을 피하려면 파일에 영속화
    _TOKEN_STORE = Path(__file__).resolve().parents[1] / ".kis_tokens.json"

    # KIS 일반 API는 초당 1회. 모든 HTTP 호출을 전역 락으로 직렬화하고
    # 이전 호출 시각을 추적해 최소 1.1초 간격을 강제한다.
    _API_LOCK = threading.Lock()
    _LAST_API_CALL: float = 0.0
    _MIN_API_INTERVAL = 1.1  # seconds

    def __init__(self, market: Market = Market.KR,
                 account_type: AccountType = AccountType.MOCK) -> None:
        self.market = market
        self.account_type = account_type
        self._token: _Token | None = None
        self._app_key: str | None = None
        self._app_secret: str | None = None
        self._cano: str | None = None
        self._acnt_prdt_cd: str | None = None
        self._session = requests.Session()

    # ─── 공용 ───

    @property
    def base_url(self) -> str:
        return self.BASE_URL_REAL if self.account_type == AccountType.REAL else self.BASE_URL_MOCK

    @property
    def is_mock(self) -> bool:
        return self.account_type == AccountType.MOCK

    def connect(self, credentials: dict[str, str]) -> None:
        app_key = credentials.get("appKey")
        app_secret = credentials.get("appSecret")
        account_no = credentials.get("accountNo")
        if not app_key or not app_secret or not account_no:
            raise ValueError("KIS: appKey / appSecret / accountNo 필수")

        self._app_key = app_key
        self._app_secret = app_secret
        self._cano, self._acnt_prdt_cd = self._split_account(account_no)

        log.info("KIS 연결 시도 type=%s account=%s-%s",
                 self.account_type, self._cano, self._acnt_prdt_cd)
        self._fetch_token()
        log.info("KIS 토큰 발급 완료")

    def disconnect(self) -> None:
        log.info("KIS 연결 해제")
        # 인스턴스 참조만 끊고 토큰 캐시는 유지 (다른 인스턴스가 재사용)
        self._token = None

    # ─── 인증 ───

    def _fetch_token(self) -> None:
        """access_token 발급. 메모리 캐시 → 디스크 캐시 → 신규 발급 순으로 조회하며,
        클래스 락으로 동시 호출을 직렬화한다. 디스크 캐시는 프로세스 재시작 시에도
        토큰을 재사용해 KIS의 1분 발급 제한을 우회한다.
        """
        assert self._app_key is not None
        cache_key = (self._app_key, self.account_type.value)

        # fast path: 락 없이 메모리 캐시 히트
        cached = self._TOKEN_CACHE.get(cache_key)
        if cached and time.time() < cached.expires_at:
            self._token = cached
            return

        with self._TOKEN_LOCK:
            # 락 대기 중 다른 스레드가 발급받았을 수 있음
            cached = self._TOKEN_CACHE.get(cache_key)
            if cached and time.time() < cached.expires_at:
                self._token = cached
                return

            # 디스크 캐시 시도
            disk_token = self._load_disk_token(cache_key)
            if disk_token and time.time() < disk_token.expires_at:
                self._token = disk_token
                self._TOKEN_CACHE[cache_key] = disk_token
                log.info("KIS 토큰 디스크 캐시 재사용 (%s, %ds 남음)",
                         self.account_type.value,
                         int(disk_token.expires_at - time.time()))
                return

            # 실제 발급
            url = f"{self.base_url}/oauth2/tokenP"
            body = {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            }
            resp = self._api_call("POST", url, json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"KIS 토큰 발급 실패: {resp.status_code} {resp.text}")
            data = resp.json()
            expires_in = int(data.get("expires_in", 86400))
            token = _Token(
                value=data["access_token"],
                expires_at=time.time() + max(0, expires_in - 300),
            )
            self._token = token
            self._TOKEN_CACHE[cache_key] = token
            self._save_disk_token(cache_key, token)
            log.info("KIS 토큰 신규 발급 (%s)", self.account_type.value)

    @classmethod
    def _load_disk_token(cls, cache_key: tuple[str, str]) -> _Token | None:
        try:
            if not cls._TOKEN_STORE.exists():
                return None
            raw = json.loads(cls._TOKEN_STORE.read_text(encoding="utf-8"))
            key_str = f"{cache_key[0]}|{cache_key[1]}"
            entry = raw.get(key_str)
            if not entry:
                return None
            return _Token(value=entry["value"], expires_at=float(entry["expires_at"]))
        except Exception as e:
            log.debug("디스크 토큰 로드 실패: %s", e)
            return None

    @classmethod
    def _save_disk_token(cls, cache_key: tuple[str, str], token: _Token) -> None:
        try:
            raw: dict[str, Any] = {}
            if cls._TOKEN_STORE.exists():
                raw = json.loads(cls._TOKEN_STORE.read_text(encoding="utf-8"))
            raw[f"{cache_key[0]}|{cache_key[1]}"] = asdict(token)
            cls._TOKEN_STORE.write_text(json.dumps(raw), encoding="utf-8")
        except Exception as e:
            log.warning("디스크 토큰 저장 실패: %s", e)

    def _auth_headers(self, tr_id: str) -> dict[str, str]:
        self._ensure_token()
        assert self._token is not None
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token.value}",
            "appkey": self._app_key or "",
            "appsecret": self._app_secret or "",
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }

    def _ensure_token(self) -> None:
        if self._token is None or time.time() >= self._token.expires_at:
            self._fetch_token()

    # ─── HTTP 직렬화 ───

    def _api_call(self, method: str, url: str, **kwargs) -> requests.Response:
        """모든 KIS HTTP 호출을 직렬화 + 최소 간격 보장.

        클래스 락을 잡는 동안 이전 호출 시각을 확인해, 필요하면 sleep으로 보충한다.
        여러 브로커 인스턴스·여러 스레드가 동시 호출해도 초당 1회 제한을 지킨다.
        """
        with self.__class__._API_LOCK:
            elapsed = time.time() - self.__class__._LAST_API_CALL
            wait = self._MIN_API_INTERVAL - elapsed
            if wait > 0:
                time.sleep(wait)
            try:
                kwargs.setdefault("timeout", self.TIMEOUT)
                return self._session.request(method, url, **kwargs)
            finally:
                self.__class__._LAST_API_CALL = time.time()

    @staticmethod
    def _split_account(account_no: str) -> tuple[str, str]:
        raw = account_no.replace("-", "").strip()
        if len(raw) < 10:
            raise ValueError(f"계좌번호 포맷 오류 (8자리-2자리 형태 기대): {account_no}")
        return raw[:8], raw[8:10]

    # ─── 잔고 ───

    def get_balance(self) -> Balance:
        self._ensure_connected()
        # 국내주식 잔고 조회: 실계좌 TTTC8434R / 모의 VTTC8434R
        tr_id = "VTTC8434R" if self.is_mock else "TTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = self._api_call('GET', url, headers=self._auth_headers(tr_id), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 잔고 조회 실패: {resp.status_code} {resp.text}")

        payload = resp.json()
        positions: list[Position] = []
        for row in payload.get("output1", []):
            qty = int(row.get("hldg_qty") or 0)
            if qty <= 0:
                continue
            positions.append(Position(
                stock_code=row.get("pdno", ""),
                stock_name=row.get("prdt_name", ""),
                quantity=qty,
                avg_price=float(row.get("pchs_avg_pric") or 0),
                current_price=float(row.get("prpr") or 0),
            ))

        summary = (payload.get("output2") or [{}])[0]
        cash = float(summary.get("dnca_tot_amt") or 0)          # 예수금 총액
        total_eval = float(summary.get("tot_evlu_amt") or 0)    # 총 평가금액
        locked = max(0.0, total_eval - cash - sum(p.eval_amount for p in positions))

        return Balance(
            cash=cash,
            locked=locked,
            total_eval=total_eval,
            currency="KRW",
            positions=positions,
        )

    # ─── 시세 ───

    def get_current_price(self, stock_code: str) -> float:
        self._ensure_connected()
        # 주식현재가 시세: 실/모의 공용 FHKST01010100
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        resp = self._api_call('GET', url, headers=self._auth_headers("FHKST01010100"), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 시세 조회 실패: {resp.status_code} {resp.text}")
        data = resp.json().get("output") or {}
        return float(data.get("stck_prpr") or 0)

    def get_kospi_change_rate(self) -> float | None:
        """KOSPI 추종 ETF KODEX 200(069500)의 전일 대비 등락률을 KOSPI 대용으로 반환.

        2026-05-13 도입. KIS inquire-price 응답의 prdy_ctrt 필드(전일 대비 등락률, %).
        실패 시 None — 호출 측이 None일 때는 약세장 가드 미적용.
        """
        try:
            self._ensure_connected()
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
            params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "069500"}
            resp = self._api_call('GET', url, headers=self._auth_headers("FHKST01010100"), params=params)
            if resp.status_code != 200:
                return None
            data = resp.json().get("output") or {}
            prdy_ctrt = data.get("prdy_ctrt")  # 전일 대비 등락률, % 단위 문자열 (예 "-1.23")
            if prdy_ctrt is None or prdy_ctrt == "":
                return None
            return float(prdy_ctrt) / 100.0  # 소수로 변환 (-1.23% → -0.0123)
        except Exception as e:
            log.debug("[KIS] KOSPI 등락률 조회 실패: %s", e)
            return None

    def get_daily_closes(self, stock_code: str, days: int = 30) -> list[float]:
        """최근 days일 종가 리스트 (오래된 → 최신 순). 기술 지표(SMA·RSI) 계산용.

        TR FHKST01010400: 주식현재가 일자별 조회 (일·주·월·년). 실/모의 공용.
        최대 30일 정도 반환. 실패 시 빈 리스트.
        """
        self._ensure_connected()
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_PERIOD_DIV_CODE": "D",  # D=일, W=주, M=월
            "FID_ORG_ADJ_PRC": "0",       # 수정주가 반영
        }
        try:
            resp = self._api_call('GET', url, headers=self._auth_headers("FHKST01010400"), params=params)
        except Exception as e:
            log.debug("[KIS] get_daily_closes 호출 실패 %s: %s", stock_code, e)
            return []
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("rt_cd") != "0":
            return []
        rows = data.get("output") or []
        # 최신 → 오래된 순으로 오므로 뒤집어서 반환
        closes: list[float] = []
        for r in rows[:days]:
            v = _to_int_safe(r.get("stck_clpr"))
            if v > 0:
                closes.append(float(v))
        closes.reverse()
        return closes

    # ─── 시장 스크리닝 (KRX) ───

    def get_volume_rankers(self, top_n: int = 30) -> list[dict[str, Any]]:
        """거래량 상위 종목 조회 (TR FHPST01710000, 실·모의 공용).

        반환 필드: stock_code, stock_name, rank, price, change_rate, volume, trade_amount
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",     # 전체
            "FID_DIV_CLS_CODE": "0",      # 전체
            "FID_BLNG_CLS_CODE": "0",     # 평균거래량
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        resp = self._api_call('GET', url, headers=self._auth_headers("FHPST01710000"), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 거래량 상위 실패: {resp.status_code} {resp.text}")

        rows = resp.json().get("output") or []
        result: list[dict[str, Any]] = []
        for i, r in enumerate(rows[:top_n], start=1):
            price = float(r.get("stck_prpr") or 0)
            prev_close = float(r.get("prdy_vrss") or 0)  # 전일대비 (부호 포함)
            change_rate = float(r.get("prdy_ctrt") or 0) / 100.0  # 전일대비율 (%)
            result.append({
                "stock_code": (r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd") or "").strip(),
                "stock_name": (r.get("hts_kor_isnm") or "").strip(),
                "rank": i,
                "price": price,
                "change_rate": change_rate,
                "volume": int(r.get("acml_vol") or 0),
                "trade_amount": float(r.get("acml_tr_pbmn") or 0),
            })
        return result

    def get_price_change_rankers(self, top_n: int = 30) -> list[dict[str, Any]]:
        """등락률 상위 (상승 기준) 조회 (TR FHPST01700000)."""
        url = f"{self.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20170",
            "FID_INPUT_ISCD": "0000",
            "FID_RANK_SORT_CLS_CODE": "0",   # 상승률
            "FID_INPUT_CNT_1": "0",
            "FID_PRC_CLS_CODE": "0",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_DIV_CLS_CODE": "0",
            "FID_RSFL_RATE1": "",
            "FID_RSFL_RATE2": "",
        }
        resp = self._api_call('GET', url, headers=self._auth_headers("FHPST01700000"), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 등락률 상위 실패: {resp.status_code} {resp.text}")

        rows = resp.json().get("output") or []
        result: list[dict[str, Any]] = []
        for i, r in enumerate(rows[:top_n], start=1):
            result.append({
                "stock_code": (r.get("stck_shrn_iscd") or "").strip(),
                "stock_name": (r.get("hts_kor_isnm") or "").strip(),
                "rank": i,
                "price": float(r.get("stck_prpr") or 0),
                "change_rate": float(r.get("prdy_ctrt") or 0) / 100.0,
                "volume": int(r.get("acml_vol") or 0),
                "trade_amount": float(r.get("acml_tr_pbmn") or 0),
            })
        return result

    def get_investor_flow(self, stock_code: str) -> dict[str, int]:
        """종목별 투자자별 매매동향 (TR FHKST01010900).

        반환 필드: foreign_net_qty, institution_net_qty, individual_net_qty, program_net_qty
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        resp = self._api_call('GET', url, headers=self._auth_headers("FHKST01010900"), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 수급 조회 실패: {resp.status_code} {resp.text}")

        rows = resp.json().get("output") or []
        if not rows:
            return {"foreign_net_qty": 0, "institution_net_qty": 0,
                    "individual_net_qty": 0, "program_net_qty": 0}
        # 가장 최근 1건만 사용
        r = rows[0]
        return {
            "foreign_net_qty": _to_int_safe(r.get("frgn_ntby_qty")),
            "institution_net_qty": _to_int_safe(r.get("orgn_ntby_qty")),
            "individual_net_qty": _to_int_safe(r.get("prsn_ntby_qty")),
            "program_net_qty": _to_int_safe(r.get("prpr_prgm_ntby_qty")),
        }

    # ─── 주문 ───

    def place_buy(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        return self._place_order(stock_code, quantity, price, OrderSide.BUY)

    def place_sell(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        return self._place_order(stock_code, quantity, price, OrderSide.SELL)

    def _place_order(self, stock_code: str, quantity: int,
                     price: float | None, side: OrderSide) -> OrderResult:
        self._ensure_connected()
        # 주식주문 (현금): 매수 실=TTTC0802U / 모의=VTTC0802U, 매도 실=TTTC0801U / 모의=VTTC0801U
        if side == OrderSide.BUY:
            tr_id = "VTTC0802U" if self.is_mock else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.is_mock else "TTTC0801U"

        # 주문 구분: 시장가 "01", 지정가 "00"
        ord_dvsn = "01" if price is None else "00"
        ord_unpr = "0" if price is None else str(int(price))

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": stock_code,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": ord_unpr,
        }
        resp = self._api_call('POST', url, headers=self._auth_headers(tr_id), json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 주문 실패: {resp.status_code} {resp.text}")

        data = resp.json()
        ok = data.get("rt_cd") == "0"
        output = data.get("output") or {}
        order_id = f"{output.get('KRX_FWDG_ORD_ORGNO', '')}-{output.get('ODNO', '')}".strip("-")

        log.info("[KIS] %s %s x%d @ %s → %s", side.value, stock_code, quantity,
                 price or "시장가", order_id or data.get("msg1"))

        return OrderResult(
            order_id=order_id,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            price=price or 0.0,
            filled=ok,
            raw=data,
        )

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_connected()
        # 정정·취소 API는 원주문번호 분해 + 잔량 조회가 필요 — 초기 구현에선 미지원
        # TODO: inquire-psbl-rvsecncl 호출 후 uapi/domestic-stock/v1/trading/order-rvsecncl
        log.warning("KIS cancel_order 미구현 (order_id=%s)", order_id)
        return False

    def get_orderable_cash(self) -> float:
        """주식주문가능금액 조회 (inquire-psbl-order).

        KIS 모의에서 매도 대금이 locked로 묶여 Balance.cash가 음수일 때에도
        실제 매수 가능액을 정확히 반환한다. `nrcvb_buy_amt` (미수없는매수금액) 사용.
        """
        self._ensure_connected()
        tr_id = "VTTC8908R" if self.is_mock else "TTTC8908R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": "",          # 공백 = 계좌 전체 기준
            "ORD_UNPR": "0",     # 시장가 기준
            "ORD_DVSN": "01",    # 시장가
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        try:
            resp = self._api_call('GET', url, headers=self._auth_headers(tr_id), params=params)
        except Exception as e:
            log.warning("[KIS] get_orderable_cash 호출 실패 → 0 폴백: %s", e)
            return 0.0

        if resp.status_code != 200:
            log.warning("[KIS] get_orderable_cash status=%s body=%s",
                        resp.status_code, resp.text[:120])
            return 0.0

        data = resp.json()
        if data.get("rt_cd") != "0":
            log.warning("[KIS] get_orderable_cash rt_cd=%s msg=%s",
                        data.get("rt_cd"), data.get("msg1"))
            return 0.0

        output = data.get("output") or {}
        # nrcvb_buy_amt: 미수없는매수금액. 없으면 ord_psbl_cash 사용.
        raw = output.get("nrcvb_buy_amt") or output.get("ord_psbl_cash") or "0"
        amount = float(_to_int_safe(raw))
        log.debug("[KIS] 주문가능금액 조회: %.0f원", amount)
        return amount

    def get_order_fill(self, order_id: str, stock_code: str) -> OrderFill | None:
        """주식일별주문체결조회(inquire-daily-ccld)로 실제 체결 평균가 확정.

        order_id 포맷: "{KRX_FWDG_ORD_ORGNO}-{ODNO}" (place_order에서 조합).
        여기서 ODNO만 뽑아 매칭한다.
        """
        self._ensure_connected()
        if not order_id:
            return None
        odno = order_id.split("-")[-1].strip()
        if not odno:
            return None

        tr_id = "VTTC0081R" if self.is_mock else "TTTC8001R"
        today = time.strftime("%Y%m%d")
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": stock_code or "",
            "CCLD_DVSN": "01",        # 체결된 것만
            "ORD_GNO_BRNO": "",
            "ODNO": odno,
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            resp = self._api_call('GET', url, headers=self._auth_headers(tr_id), params=params)
        except Exception as e:
            log.debug("[KIS] get_order_fill 호출 실패 (%s): %s", order_id, e)
            return None

        if resp.status_code != 200:
            log.debug("[KIS] get_order_fill status=%s body=%s",
                      resp.status_code, resp.text[:120])
            return None

        data = resp.json()
        if data.get("rt_cd") != "0":
            log.debug("[KIS] get_order_fill rt_cd=%s msg=%s",
                      data.get("rt_cd"), data.get("msg1"))
            return None

        rows = data.get("output1") or []
        # ODNO가 정확히 일치하는 행만
        matched = [r for r in rows if str(r.get("odno", "")).strip() == odno]
        if not matched:
            return None

        total_qty = sum(_to_int_safe(r.get("tot_ccld_qty")) for r in matched)
        total_amt = sum(_to_int_safe(r.get("tot_ccld_amt")) for r in matched)
        if total_qty <= 0 or total_amt <= 0:
            return None
        avg_price = total_amt / total_qty

        return OrderFill(
            order_id=order_id,
            stock_code=stock_code,
            filled_quantity=total_qty,
            avg_fill_price=avg_price,
            total_fill_amount=float(total_amt),
        )

    # ─── helpers ───

    def _ensure_connected(self) -> None:
        if self._token is None:
            raise RuntimeError("KIS 브로커가 연결되지 않았습니다. connect() 먼저 호출하세요.")


def _to_int_safe(value: Any) -> int:
    """KIS 응답은 문자열 정수(부호 포함, 공백/콤마 섞임) 형식이라 안전 변환."""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0

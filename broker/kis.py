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

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .base import (
    AccountType,
    BaseBroker,
    Balance,
    BrokerName,
    Market,
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
    """한국투자증권 KIS Developers REST 연동."""

    name = BrokerName.KIS

    BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
    BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"

    TIMEOUT = 10  # seconds

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
        self._token = None

    # ─── 인증 ───

    def _fetch_token(self) -> None:
        """access_token 발급. 하루 1회 호출 권장 (KIS는 빈번 호출 시 오류 반환)."""
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        resp = self._session.post(url, json=body, timeout=self.TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 토큰 발급 실패: {resp.status_code} {resp.text}")
        data = resp.json()
        # expires_in: 초 단위. 보수적으로 5분 일찍 만료 처리
        expires_in = int(data.get("expires_in", 86400))
        self._token = _Token(
            value=data["access_token"],
            expires_at=time.time() + max(0, expires_in - 300),
        )

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
        resp = self._session.get(url, headers=self._auth_headers(tr_id),
                                 params=params, timeout=self.TIMEOUT)
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
        resp = self._session.get(url, headers=self._auth_headers("FHKST01010100"),
                                 params=params, timeout=self.TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"KIS 시세 조회 실패: {resp.status_code} {resp.text}")
        data = resp.json().get("output") or {}
        return float(data.get("stck_prpr") or 0)

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
        resp = self._session.post(url, headers=self._auth_headers(tr_id),
                                  json=body, timeout=self.TIMEOUT)
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

    # ─── helpers ───

    def _ensure_connected(self) -> None:
        if self._token is None:
            raise RuntimeError("KIS 브로커가 연결되지 않았습니다. connect() 먼저 호출하세요.")

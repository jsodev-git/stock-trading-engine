"""키움 OpenAPI+ 브로커 구현.

Windows COM 기반이라 PyQt5 이벤트 루프 내에서 동작시킨다.
pykiwoom 라이브러리는 CommConnect(로그인)·OnEventConnect·SendOrder 등을 래핑.

credentials 스키마: {"accountNo": "...", "accountPassword": "..."}
  - accountNo: 10자리 (예: "1234567890"). 모의/실계좌 여부는 account_type으로 구분.
  - accountPassword: HTS 계좌 비밀번호 (4자리).

주의:
- 모의투자/실거래 분기는 계좌번호만으로 이루어지며, 키움 HTS 로그인 시
  "모의투자 접속" 체크박스를 수동으로 한 번 활성화해 둬야 한다.
- OpenAPI+ 수동 버전 업데이트가 주기적으로 필요.
- PyQt5 QAxWidget은 단일 스레드 COM이라 run_cycle 내에서만 사용.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

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

# pykiwoom은 Windows + 키움 OpenAPI+ 설치 환경에서만 import 가능.
# 개발환경(비Windows) 테스트를 위해 lazy import.
try:
    from pykiwoom.kiwoom import Kiwoom  # type: ignore
    _PYKIWOOM_AVAILABLE = True
except Exception:  # pragma: no cover
    _PYKIWOOM_AVAILABLE = False
    Kiwoom = None  # type: ignore


# 주문 타입 (SendOrder 4번째 인자)
_ORDER_TYPE_BUY = 1
_ORDER_TYPE_SELL = 2
# _ORDER_TYPE_CANCEL_BUY = 3  # 정정·취소는 추후 구현

# 호가구분 (SendOrder 7번째 인자)
_HOGA_MARKET = "03"   # 시장가
_HOGA_LIMIT = "00"    # 지정가


class KiwoomBroker(BaseBroker):
    """키움 OpenAPI+ 연동. Windows 전용."""

    name = BrokerName.KIWOOM

    def __init__(self, market: Market = Market.KR,
                 account_type: AccountType = AccountType.MOCK) -> None:
        self.market = market
        self.account_type = account_type
        self._kiwoom: Optional["Kiwoom"] = None
        self._account_no: Optional[str] = None
        self._password: Optional[str] = None

    # ─── 연결 ───

    def connect(self, credentials: dict[str, str]) -> None:
        if not _PYKIWOOM_AVAILABLE:
            raise RuntimeError(
                "pykiwoom을 import하지 못했습니다. Windows + 키움 OpenAPI+ 설치 환경에서만 동작합니다."
            )

        account_no = credentials.get("accountNo")
        password = credentials.get("accountPassword")
        if not account_no or not password:
            raise ValueError("KIWOOM: accountNo / accountPassword 필수")

        self._account_no = account_no.replace("-", "").strip()
        self._password = password

        log.info("키움 연결 시도 account=%s type=%s", self._account_no, self.account_type)
        self._kiwoom = Kiwoom()
        self._kiwoom.CommConnect(block=True)  # 로그인 창 팝업 → 사용자 입력 또는 자동 로그인

        server = self._kiwoom.GetLoginInfo("GetServerGubun")  # 모의=1 / 실=실제계정
        is_mock_server = server == "1"
        expected_mock = self.account_type == AccountType.MOCK
        if is_mock_server != expected_mock:
            self._kiwoom.CommTerminate()
            self._kiwoom = None
            raise RuntimeError(
                f"계좌 구분 불일치: 로그인 서버={'모의' if is_mock_server else '실계좌'}, "
                f"요청={self.account_type}. HTS에서 모의투자 접속 설정을 확인하세요."
            )

        accounts = (self._kiwoom.GetLoginInfo("ACCNO") or "").split(";")
        accounts = [a for a in accounts if a]
        if self._account_no not in accounts:
            available = ", ".join(accounts) or "(없음)"
            self._kiwoom.CommTerminate()
            self._kiwoom = None
            raise RuntimeError(
                f"로그인된 사용자의 계좌 목록({available})에 {self._account_no} 없음"
            )
        log.info("키움 로그인 완료 — 계좌 %s", self._account_no)

    def disconnect(self) -> None:
        if self._kiwoom is not None:
            try:
                self._kiwoom.CommTerminate()
            except Exception as e:  # pragma: no cover
                log.warning("키움 연결 해제 중 오류: %s", e)
        self._kiwoom = None
        self._account_no = None
        self._password = None
        log.info("키움 연결 해제")

    # ─── 잔고 ───

    def get_balance(self) -> Balance:
        kw = self._ensure_connected()

        # opw00018 — 계좌평가잔고내역요청
        kw.SetInputValue("계좌번호", self._account_no)
        kw.SetInputValue("비밀번호", self._password)
        kw.SetInputValue("비밀번호입력매체구분", "00")
        kw.SetInputValue("조회구분", "1")
        df_list = kw.CommRqData("opw00018_req", "opw00018", 0, "0101")
        # pykiwoom >= 0.3: block_request로 받아야 정상. 아래는 block_request 방식 호환.
        # 위 호출이 실패하는 경우 block_request 사용으로 대체.
        if df_list is None:
            df_list = kw.block_request(
                "opw00018",
                계좌번호=self._account_no,
                비밀번호=self._password,
                비밀번호입력매체구분="00",
                조회구분="1",
                output="계좌평가잔고개별합산",
                next=0,
            )

        time.sleep(0.3)  # TR 요청 간격 제한 (5회/초) 대응

        # 다음 output — 종목별 보유내역
        df_items = kw.block_request(
            "opw00018",
            계좌번호=self._account_no,
            비밀번호=self._password,
            비밀번호입력매체구분="00",
            조회구분="1",
            output="계좌평가잔고개별합산",
            next=2,
        )
        time.sleep(0.3)

        positions: list[Position] = []
        if df_items is not None and not df_items.empty:
            for _, row in df_items.iterrows():
                qty = int(_to_int(row.get("보유수량")))
                if qty <= 0:
                    continue
                positions.append(Position(
                    stock_code=str(row.get("종목번호", "")).lstrip("A").strip(),
                    stock_name=str(row.get("종목명", "")).strip(),
                    quantity=qty,
                    avg_price=float(_to_int(row.get("매입가"))),
                    current_price=float(_to_int(row.get("현재가"))),
                ))

        cash = 0.0
        total_eval = 0.0
        if df_list is not None and not df_list.empty:
            row = df_list.iloc[0]
            cash = float(_to_int(row.get("예수금") or row.get("d+2추정예수금") or 0))
            total_eval = float(_to_int(row.get("총평가금액") or 0))

        locked = max(0.0, total_eval - cash - sum(p.eval_amount for p in positions))

        return Balance(
            cash=cash, locked=locked, total_eval=total_eval,
            currency="KRW", positions=positions,
        )

    # ─── 시세 ───

    def get_current_price(self, stock_code: str) -> float:
        kw = self._ensure_connected()
        # opt10001 — 주식기본정보요청
        df = kw.block_request(
            "opt10001",
            종목코드=stock_code,
            output="주식기본정보",
            next=0,
        )
        time.sleep(0.3)
        if df is None or df.empty:
            return 0.0
        price = _to_int(df.iloc[0].get("현재가"))
        return float(abs(price))  # 키움은 등락방향에 따라 부호를 붙이므로 절대값

    # ─── 주문 ───

    def place_buy(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        return self._place_order(stock_code, quantity, price, OrderSide.BUY)

    def place_sell(self, stock_code: str, quantity: int, price: float | None = None) -> OrderResult:
        return self._place_order(stock_code, quantity, price, OrderSide.SELL)

    def _place_order(self, stock_code: str, quantity: int,
                     price: float | None, side: OrderSide) -> OrderResult:
        kw = self._ensure_connected()
        order_type = _ORDER_TYPE_BUY if side == OrderSide.BUY else _ORDER_TYPE_SELL
        hoga = _HOGA_MARKET if price is None else _HOGA_LIMIT
        price_int = 0 if price is None else int(price)

        # SendOrder(사용자구분요청명, 화면번호, 계좌번호, 주문유형, 종목코드, 주문수량, 주문가격, 거래구분, 원주문번호)
        result = kw.SendOrder(
            f"{side.value}_{stock_code}",
            "0101",
            self._account_no,
            order_type,
            stock_code,
            quantity,
            price_int,
            hoga,
            "",
        )
        success = result == 0
        log.info("[키움] %s %s x%d @ %s → rc=%s",
                 side.value, stock_code, quantity, price or "시장가", result)

        # SendOrder는 주문 요청만 전송. 주문번호는 OnReceiveChejanData 이벤트로 수신.
        # pykiwoom은 이벤트 수신을 동기적으로 기다리는 유틸을 제공하지 않으므로
        # 실제 주문번호는 이후 미체결 조회(opt10075)로 매칭한다 — 현재는 빈값 반환.
        return OrderResult(
            order_id="",
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            price=price or 0.0,
            filled=success,
            raw={"rc": result},
        )

    def cancel_order(self, order_id: str) -> bool:
        # TODO: opt10075(실시간미체결요청) 조회 후 SendOrder 취소
        log.warning("키움 cancel_order 미구현 (order_id=%s)", order_id)
        return False

    # ─── helpers ───

    def _ensure_connected(self) -> "Kiwoom":
        if self._kiwoom is None:
            raise RuntimeError("키움 브로커가 연결되지 않았습니다. connect() 먼저 호출하세요.")
        return self._kiwoom


def _to_int(value) -> int:
    """키움 응답 필드는 문자열로 숫자(앞에 +/- 붙을 수 있음)를 담고 있다."""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "").replace("+", "")
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0

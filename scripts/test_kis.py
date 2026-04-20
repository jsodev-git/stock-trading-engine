"""KIS Developers 계좌 연결 단독 테스트.

사용법:
    python scripts/test_kis.py --mock        # 모의투자 (기본)
    python scripts/test_kis.py --real        # 실거래

환경변수 (또는 --app-key / --app-secret / --account-no 플래그로 전달):
    KIS_APP_KEY       앱키
    KIS_APP_SECRET    앱시크릿
    KIS_ACCOUNT_NO    계좌번호 (형식: 12345678-01 또는 1234567801)

확인 항목:
    1. OAuth access_token 발급
    2. 잔고 조회 (예수금 / 총평가 / 보유 종목 수)
    3. 삼성전자(005930) 현재가 조회
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Windows 기본 콘솔(cp949)에서 한글/이모지가 깨지지 않도록 UTF-8 강제
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# trading/ 을 sys.path 에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker import get_broker
from broker.base import AccountType  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS 계좌 연결 단독 테스트")
    parser.add_argument("--real", action="store_true", help="실거래 계좌로 테스트")
    parser.add_argument("--mock", action="store_true", help="모의투자 계좌로 테스트 (기본)")
    parser.add_argument("--app-key", help="AppKey (또는 KIS_APP_KEY 환경변수)")
    parser.add_argument("--app-secret", help="AppSecret (또는 KIS_APP_SECRET 환경변수)")
    parser.add_argument("--account-no", help="계좌번호 (또는 KIS_ACCOUNT_NO 환경변수)")
    parser.add_argument("--stock-code", default="005930",
                        help="현재가 조회할 종목코드 (기본: 005930 삼성전자)")
    args = parser.parse_args()

    account_type = AccountType.REAL if args.real else AccountType.MOCK

    app_key = args.app_key or os.getenv("KIS_APP_KEY")
    app_secret = args.app_secret or os.getenv("KIS_APP_SECRET")
    account_no = args.account_no or os.getenv("KIS_ACCOUNT_NO")

    missing = [n for n, v in [("APP_KEY", app_key), ("APP_SECRET", app_secret),
                               ("ACCOUNT_NO", account_no)] if not v]
    if missing:
        print(f"[!] 누락된 값: {', '.join(missing)}", file=sys.stderr)
        print("    --app-key / --app-secret / --account-no 플래그나 환경변수로 전달하세요.",
              file=sys.stderr)
        return 1

    print(f"[*] KIS {account_type.value} 테스트 시작 — 계좌 {account_no}")

    broker = get_broker("KIS", market="KR", account_type=account_type)
    try:
        broker.connect({
            "appKey": app_key,
            "appSecret": app_secret,
            "accountNo": account_no,
        })
        print("[OK] OAuth 토큰 발급 완료")

        balance = broker.get_balance()
        print(f"[OK] 잔고 조회")
        print(f"     예수금     : {balance.cash:,.0f} KRW")
        print(f"     주문중     : {balance.locked:,.0f} KRW")
        print(f"     총 평가    : {balance.total_eval:,.0f} KRW")
        print(f"     보유 종목  : {len(balance.positions)}개")
        for p in balance.positions:
            print(f"       - {p.stock_code} {p.stock_name} x{p.quantity} "
                  f"(avg={p.avg_price:.0f}, now={p.current_price:.0f}, "
                  f"pnl={p.profit_rate*100:+.2f}%)")

        price = broker.get_current_price(args.stock_code)
        print(f"[OK] {args.stock_code} 현재가 : {price:,.0f} KRW")

    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        broker.disconnect()

    print("[DONE] 모든 호출 성공")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""find_stocks.py 가 발굴한 상위 3종목을 모의투자 계좌에서 시장가 매수.

매수 대상:
    - 000660 SK하이닉스
    - 005380 현대차
    - 066570 LG전자

각 종목당 1주씩, 시장가(ORD_DVSN=01, 가격=0) 로 주문한다.
KIS Open API 의 초당 호출 제한을 회피하기 위해 모든 API 호출 사이에
time.sleep(0.5) 를 적용한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pykis import Api, DomainInfo

from config import load_config

# 모든 API 호출 사이에 적용할 최소 간격 (초)
RATE_LIMIT_SLEEP = 0.5

# 종목당 매수 수량
ORDER_QTY = 1

# 매수 대상: (종목코드, 표시용 종목명)
TARGETS: list[tuple[str, str]] = [
    ("000660", "SK하이닉스"),
    ("005380", "현대차"),
    ("066570", "LG전자"),
]


@dataclass
class OrderResult:
    ticker: str
    name: str
    success: bool
    order_no: str = ""
    order_time: str = ""
    org_no: str = ""
    error: str = ""
    ref_price: int = 0


def throttled_sleep() -> None:
    """다음 API 호출 전 일정 간격 대기."""
    time.sleep(RATE_LIMIT_SLEEP)


def get_current_price(api: Api, ticker: str) -> int:
    """현재가 조회 (참고용)."""
    info = api._get_kr_stock_current_price_info(ticker)
    return int(info["stck_prpr"])


def market_buy(api: Api, ticker: str, qty: int) -> dict[str, Any]:
    """시장가 매수. pykis 0.7.0 은 price<=0 일 때 ORD_DVSN=01(시장가) 로 전송."""
    return api.buy_kr_stock(ticker, qty, 0)


def execute_target(api: Api, ticker: str, name: str) -> OrderResult:
    """단일 종목에 대해 [현재가 조회 → 시장가 매수] 를 실행."""
    result = OrderResult(ticker=ticker, name=name, success=False)

    throttled_sleep()
    try:
        result.ref_price = get_current_price(api, ticker)
        print(f"  현재가: {result.ref_price:,} 원")
    except Exception as exc:
        # 현재가 조회 실패는 매수 자체를 막지 않음 (시장가 주문이므로 가격 불필요)
        print(f"  현재가 조회 실패 (무시하고 진행): {exc}")

    throttled_sleep()
    try:
        resp = market_buy(api, ticker, ORDER_QTY)
        result.order_no = str(resp.get("ODNO", "")).strip()
        result.order_time = str(resp.get("ORD_TMD", "")).strip()
        result.org_no = str(resp.get("KRX_FWDG_ORD_ORGNO", "")).strip()
        if result.order_no:
            result.success = True
            print(
                f"  [성공] 주문번호 {result.order_no}"
                f" (조직 {result.org_no}, 주문시각 {result.order_time})"
            )
        else:
            result.error = f"응답에 ODNO 없음: {resp}"
            print(f"  [실패] {result.error}")
    except Exception as exc:
        result.error = str(exc)
        print(f"  [실패] {exc}")

    return result


def print_summary(results: list[OrderResult]) -> None:
    print()
    print("=" * 60)
    print("매수 주문 결과")
    print("=" * 60)
    header = (
        f"{'종목코드':<8}  {'종목명':<10}  {'수량':>4}  "
        f"{'결과':<4}  {'주문번호':<12}  {'주문시각':<8}"
    )
    print(header)
    print("-" * 60)
    for r in results:
        status = "성공" if r.success else "실패"
        ord_no = r.order_no or "-"
        ord_tmd = r.order_time or "-"
        print(
            f"{r.ticker:<8}  {r.name:<10}  {ORDER_QTY:>4}  "
            f"{status:<4}  {ord_no:<12}  {ord_tmd:<8}"
        )
    success_count = sum(1 for r in results if r.success)
    print("-" * 60)
    print(f"총 {success_count}/{len(results)} 건 주문 접수 완료")

    failures = [r for r in results if not r.success]
    if failures:
        print()
        print("실패 상세:")
        for r in failures:
            print(f"  - {r.ticker} {r.name}: {r.error}")


def main() -> None:
    cfg = load_config()
    api = Api(
        key_info={"appkey": cfg.app_key, "appsecret": cfg.app_secret},
        domain_info=DomainInfo(kind="virtual" if cfg.is_paper else "real"),
        account_info={
            "account_code": cfg.account_no,
            "product_code": cfg.account_product_code,
        },
    )

    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(f"매수 대상 {len(TARGETS)}개 종목 × {ORDER_QTY}주 시장가")
    print(f"API 호출 간격: {RATE_LIMIT_SLEEP}s")

    results: list[OrderResult] = []
    for i, (ticker, name) in enumerate(TARGETS, 1):
        print()
        print(f"[{i}/{len(TARGETS)}] {ticker} {name}")
        results.append(execute_target(api, ticker, name))

    print_summary(results)


if __name__ == "__main__":
    main()

"""auto_trader.py — 종목 스캔 → 자동 매수 → 체결 검증을 한 번에 수행하는 봇.

흐름:
    1. KOSPI 200 종목 리스트 수집 (네이버 금융 KPI200 페이지 스크레이핑)
    2. pykis 로 각 종목 시세 조회 (현재가, 52주 최고가)
       — 종목 조회 루프마다 time.sleep(0.1)
    3. 52주 최고가 대비 95% 이상 종목 중, 비율 상위 3개 추출
    4. 주문 직전 보유 잔고 스냅샷 저장
    5. 각 종목당 1주 시장가 매수 (주문 사이 time.sleep(0.5))
    6. 매수 직후 time.sleep(3) 으로 체결 대기
    7. 잔고를 재조회해 보유수량 델타로 체결 여부 검증 후 로그 출력
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from pykis import Api, DomainInfo

from config import load_config

# ── 튜닝 가능한 상수 ─────────────────────────────────────────────
SCAN_SLEEP = 0.1                 # 시세 조회 루프 간격 (초)
ORDER_SLEEP = 0.5                # 매수 주문 사이 간격 (초)
SETTLEMENT_WAIT = 3              # 매수 완료 후 체결 대기 시간 (초)
PROXIMITY_THRESHOLD = 95.0       # 52주 최고가 대비 비율 (%) 기준선
TOP_N = 3                        # 후보 중 상위 몇 개를 매수할지
ORDER_QTY = 1                    # 종목당 매수 수량

NAVER_URL = "https://finance.naver.com/sise/entryJongmok.naver"
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}

# KIS 모의투자가 ~2 req/sec 만 허용하므로, 0.1s 간격이 누적되면 일부 호출이
# "초당 거래건수를 초과하였습니다" 로 실패한다. 그때는 잠깐 쉬고 재시도한다.
RATE_LIMIT_MARKERS = ("초당 거래건수", "초당 거래 건수")
MAX_RETRIES = 4
RETRY_BACKOFFS = (1.0, 2.0, 3.0, 5.0)  # 초


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(m in msg for m in RATE_LIMIT_MARKERS)


def call_with_retry(fn, *args, label: str = "", **kwargs):
    """KIS rate limit 에러에 한해 자동 재시도. 다른 예외는 그대로 전파."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_rate_limit_error(exc):
                raise
            last_exc = exc
            backoff = RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)]
            tag = f"[{label}] " if label else ""
            print(f"  ↻ {tag}rate limit, {backoff}s 대기 후 재시도"
                  f" ({attempt + 1}/{MAX_RETRIES})")
            time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


# ── 데이터 클래스 ──────────────────────────────────────────────
@dataclass
class Snapshot:
    """단일 종목의 시세 스냅샷."""
    ticker: str
    name: str
    current: int
    high_52w: int

    @property
    def pct_of_high(self) -> float:
        return self.current / self.high_52w * 100.0


@dataclass
class OrderResult:
    """매수 주문 1건의 접수 결과."""
    ticker: str
    name: str
    accepted: bool = False
    order_no: str = ""
    order_time: str = ""
    error: str = ""


@dataclass
class FillResult:
    """체결 검증 결과."""
    ticker: str
    name: str
    qty_before: int
    qty_after: int
    avg_price_after: float = 0.0
    filled: bool = False

    @property
    def delta(self) -> int:
        return self.qty_after - self.qty_before


# ── KOSPI 200 종목 리스트 ───────────────────────────────────────
def fetch_kospi200_tickers() -> list[tuple[str, str]]:
    """네이버 금융 KPI200 페이지에서 (종목코드, 종목명) 전체 수집."""
    pattern = re.compile(
        r'/item/main\.naver\?code=(?P<code>\d{6})"[^>]*>(?P<name>[^<]+)</a>'
    )
    pairs: dict[str, str] = {}
    for page in range(1, 23):  # 200종목 / 10per page = 20p, 여유로 22까지 시도
        resp = requests.get(
            NAVER_URL,
            params={"code": "KPI200", "page": page},
            headers=NAVER_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        before = len(pairs)
        for m in pattern.finditer(resp.text):
            pairs.setdefault(m["code"], m["name"].strip())
        if len(pairs) == before:
            break  # 마지막 페이지 도달
    return list(pairs.items())


# ── pykis Api 빌더 ────────────────────────────────────────────
def build_api():
    cfg = load_config()
    api = Api(
        key_info={"appkey": cfg.app_key, "appsecret": cfg.app_secret},
        domain_info=DomainInfo(kind="virtual" if cfg.is_paper else "real"),
        account_info={
            "account_code": cfg.account_no,
            "product_code": cfg.account_product_code,
        },
    )
    return api, cfg


# ── 1단계: 스캔 ────────────────────────────────────────────────
def scan_market(api: Api, tickers: list[tuple[str, str]]) -> list[Snapshot]:
    """모든 종목의 시세 조회. 호출마다 SCAN_SLEEP 만큼 대기."""
    snaps: list[Snapshot] = []
    total = len(tickers)
    for i, (ticker, name) in enumerate(tickers, 1):
        try:
            info = call_with_retry(
                api._get_kr_stock_current_price_info, ticker, label=ticker
            )
            current = int(info["stck_prpr"])
            high_52w = int(info["w52_hgpr"])
            if high_52w > 0:
                snaps.append(Snapshot(ticker, name, current, high_52w))
        except Exception as exc:
            print(f"  ! 시세 조회 실패 {ticker} {name}: {exc}")
        if i % 30 == 0:
            print(f"      ... {i}/{total} 진행")
        time.sleep(SCAN_SLEEP)
    return snaps


# ── 2단계: 후보 필터링 ───────────────────────────────────────────
def pick_top_candidates(snaps: list[Snapshot]) -> list[Snapshot]:
    """52주 최고가의 95% 이상 종목 중 비율 상위 TOP_N 추출."""
    qualified = [s for s in snaps if s.pct_of_high >= PROXIMITY_THRESHOLD]
    qualified.sort(key=lambda s: s.pct_of_high, reverse=True)
    return qualified[:TOP_N]


# ── 잔고 헬퍼 ─────────────────────────────────────────────────
def snapshot_holdings(api: Api) -> dict[str, dict]:
    """현재 보유 종목을 {종목코드: {qty, avg_price, name}} dict 로 반환."""
    df = call_with_retry(api.get_kr_stock_balance, label="balance")
    if df is None or df.empty:
        return {}
    holdings: dict[str, dict] = {}
    for ticker, row in df.iterrows():
        holdings[str(ticker)] = {
            "name": str(row.get("종목명", "")),
            "qty": int(row.get("보유수량", 0)),
            "avg_price": float(row.get("매입단가", 0.0)),
        }
    return holdings


# ── 3단계: 매수 실행 ────────────────────────────────────────────
def execute_buys(api: Api, targets: list[Snapshot]) -> list[OrderResult]:
    """타겟 종목들을 시장가로 1주씩 매수. 주문 사이 ORDER_SLEEP 대기."""
    results: list[OrderResult] = []
    for i, t in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] 매수 시도: {t.ticker} {t.name}"
              f" (현재가 {t.current:,}, 52주고 대비 {t.pct_of_high:.2f}%)")
        result = OrderResult(ticker=t.ticker, name=t.name)
        try:
            # price=0 → pykis 가 ORD_DVSN=01(시장가) 로 변환
            resp = call_with_retry(
                api.buy_kr_stock, t.ticker, ORDER_QTY, 0, label=f"buy {t.ticker}"
            )
            result.order_no = str(resp.get("ODNO", "")).strip()
            result.order_time = str(resp.get("ORD_TMD", "")).strip()
            if result.order_no:
                result.accepted = True
                print(f"      → 접수 OK 주문번호 {result.order_no}"
                      f" (주문시각 {result.order_time})")
            else:
                result.error = f"응답에 ODNO 없음: {resp}"
                print(f"      → 접수 실패: {result.error}")
        except Exception as exc:
            result.error = str(exc)
            print(f"      → 접수 실패: {exc}")
        results.append(result)
        # 마지막 주문 뒤에도 동일 간격 유지 (다음 단계 호출 보호)
        time.sleep(ORDER_SLEEP)
    return results


# ── 4단계: 체결 검증 ────────────────────────────────────────────
def verify_fills(
    api: Api,
    targets: list[Snapshot],
    orders: list[OrderResult],
    holdings_before: dict[str, dict],
) -> list[FillResult]:
    """잔고 재조회 후 보유수량 델타로 체결 여부 판정."""
    holdings_after = snapshot_holdings(api)
    fills: list[FillResult] = []
    for t, o in zip(targets, orders):
        before = holdings_before.get(t.ticker, {"qty": 0})
        after = holdings_after.get(t.ticker, {"qty": 0, "avg_price": 0.0})
        fr = FillResult(
            ticker=t.ticker,
            name=t.name,
            qty_before=int(before["qty"]),
            qty_after=int(after["qty"]),
            avg_price_after=float(after.get("avg_price", 0.0)),
        )
        # 접수가 성공했고, 보유수량이 주문량만큼 증가했으면 체결로 판정
        fr.filled = o.accepted and (fr.delta >= ORDER_QTY)
        fills.append(fr)
    return fills


# ── 로깅 ─────────────────────────────────────────────────────
def log_candidates(snaps: list[Snapshot], picks: list[Snapshot]) -> None:
    qualified_count = sum(1 for s in snaps if s.pct_of_high >= PROXIMITY_THRESHOLD)
    print(f"      조건(≥{PROXIMITY_THRESHOLD:.0f}%) 만족: {qualified_count}개"
          f" → 상위 {len(picks)}개 선정")
    if not picks:
        return
    print(f"      {'순위':<4}{'종목코드':<10}{'종목명':<14}{'현재가':>11}"
          f"  {'52주고':>11}  {'비율':>7}")
    for rank, s in enumerate(picks, 1):
        print(f"      {rank:<4}{s.ticker:<10}{s.name:<14}"
              f"{s.current:>11,}  {s.high_52w:>11,}  {s.pct_of_high:>6.2f}%")


def log_fill_summary(fills: list[FillResult], orders: list[OrderResult]) -> None:
    print()
    print("=" * 72)
    print("최종 체결 검증 결과")
    print("=" * 72)
    header = (f"{'종목코드':<10}{'종목명':<14}{'주문번호':<14}"
              f"{'이전수량':>8}{'현재수량':>8}{'델타':>6}  {'체결':<6}")
    print(header)
    print("-" * 72)
    for f, o in zip(fills, orders):
        status = "체결완료" if f.filled else ("미체결" if o.accepted else "접수실패")
        print(f"{f.ticker:<10}{f.name:<14}{(o.order_no or '-'):<14}"
              f"{f.qty_before:>8}{f.qty_after:>8}{f.delta:>+6}  {status:<6}")
    print("-" * 72)

    filled_count = sum(1 for f in fills if f.filled)
    print(f"체결 완료: {filled_count}/{len(fills)} 종목")

    accepted_but_not_filled = [
        (f, o) for f, o in zip(fills, orders) if o.accepted and not f.filled
    ]
    if accepted_but_not_filled:
        print()
        print("주문은 접수됐으나 아직 체결 확인 안 됨 (장 마감/대기 가능):")
        for f, o in accepted_but_not_filled:
            print(f"  - {f.ticker} {f.name} 주문번호 {o.order_no}")

    failed = [(f, o) for f, o in zip(fills, orders) if not o.accepted]
    if failed:
        print()
        print("접수 자체가 실패:")
        for f, o in failed:
            print(f"  - {f.ticker} {f.name}: {o.error}")


# ── 메인 ─────────────────────────────────────────────────────
def main() -> None:
    api, cfg = build_api()
    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(f"파라미터: scan_sleep={SCAN_SLEEP}s, order_sleep={ORDER_SLEEP}s,"
          f" settlement_wait={SETTLEMENT_WAIT}s, top_n={TOP_N}, qty={ORDER_QTY}")

    print()
    print("[1/4] KOSPI 200 종목 목록 수집")
    tickers = fetch_kospi200_tickers()
    print(f"      → {len(tickers)}개 종목 확보")

    print()
    print("[2/4] 시세 스캔 (52주 최고가 대비 비율 산정)")
    snaps = scan_market(api, tickers)
    print(f"      → {len(snaps)}개 종목 시세 수집 완료")

    picks = pick_top_candidates(snaps)
    log_candidates(snaps, picks)

    if not picks:
        print("\n매수 대상이 없어 종료합니다.")
        return

    print()
    print("[3/4] 시장가 매수 주문")
    # 스캔 직후의 burst 를 피하기 위해 단계 전환 쿨다운
    time.sleep(1.0)
    # 주문 전 보유 잔고 스냅샷 (체결 검증의 기준점)
    holdings_before = snapshot_holdings(api)
    time.sleep(ORDER_SLEEP)  # 잔고 조회 직후에도 동일 간격 유지
    orders = execute_buys(api, picks)

    print()
    print(f"[4/4] 체결 대기 {SETTLEMENT_WAIT}초 후 잔고 재조회")
    time.sleep(SETTLEMENT_WAIT)
    fills = verify_fills(api, picks, orders, holdings_before)
    log_fill_summary(fills, orders)


if __name__ == "__main__":
    main()

"""peakeasy_r_bot.py — PEAK Easy R-Model 포지션 사이징 자동매매 봇.

전략 흐름:
    1. KOSPI 200 종목 리스트 수집 (네이버 금융 KPI200 스크레이핑)
    2. KOSPI 200 지수의 20거래일 수익률 계산 (RS 의 기준선)
    3. 각 종목의 40거래일 일봉을 inquire-daily-itemchartprice 로 수집
    4. 3대 발굴 필터:
       ① 가격: 현재가(당일 종가)가 52주(250일) 최고가 이상 (돌파 또는 일치, Breakout)
       ② 수급: 당일 거래량이 직전 20거래일 평균 거래량의 200%(2배) 이상
       ③ 강도(RS): (종목 20일 수익률) - (KOSPI200 20일 수익률) 상위 5개
    5. 매수 직전 총자산(예수금+주식평가금) 조회 → R-Multiple 포지션 사이징
       - 1R = TOTAL_ASSET × 1.6%
       - 종목당 매수 금액 BUY_AMOUNT = 1R / 8%  (= TOTAL_ASSET × 20%)
       - 동시 보유 한도 5종목, 빈 슬롯만큼만 상위 종목 진입
    6. 시장가(종가 베팅) 매수 — 수량 = BUY_AMOUNT // 현재가
    7. 3초 대기 후 보유수량 델타로 체결 검증, 표로 출력
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from pykis import Api, DomainInfo
from pykis.public_api import APIRequestParameter

from config import load_config
from telegram_notifier import send_message, tee_capture


# ── 전략 파라미터 ──────────────────────────────────────────────
ONE_R_RATIO = 0.016                # 총자산 대비 1R 비중 (1.6%)
STOP_LOSS_PCT = 0.08               # 고정 손절폭 (-8%)
MAX_POSITIONS = 5                  # 동시 보유 가능 종목 수
VOLUME_SURGE_RATIO = 2.0           # ② 직전 20일 평균 거래량 대비 배수
RS_LOOKBACK = 20                   # 20거래일 RS / 거래량 기준
TOP_N_BY_RS = 5                    # ③ RS 상위 N 개를 최종 타겟으로 선정

# ── 데이터 수집 파라미터 ──────────────────────────────────────
DAILY_BARS_TRADING_DAYS = 40       # 종목당 받아올 일봉 거래일수
INDEX_BARS_TRADING_DAYS = 20       # 지수에 대해 받아올 일봉 거래일수
KOSPI200_INDEX_CODE = "2001"       # 한투 OpenAPI 기준 KOSPI 200 지수 코드

# ── 인프라 상수 ────────────────────────────────────────────────
SCAN_SLEEP = 0.5                   # 종목 간 조회 인터벌 (모의투자 초당 2건 제한 준수)
ORDER_SLEEP = 0.5                  # 주문 간 인터벌
SETTLEMENT_WAIT = 3                # 매수 후 체결 대기 시간

NAVER_URL = "https://finance.naver.com/sise/entryJongmok.naver"
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}

RATE_LIMIT_MARKERS = ("초당 거래건수", "초당 거래 건수")
MAX_RETRIES = 4
RETRY_BACKOFFS = (1.0, 2.0, 3.0, 5.0)

CHART_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
CHART_TR_ID = "FHKST03010100"
INDEX_CHART_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
INDEX_CHART_TR_ID = "FHKUP03500100"


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(m in msg for m in RATE_LIMIT_MARKERS)


def call_with_retry(fn, *args, label: str = "", **kwargs):
    """KIS rate-limit 에러에 한해 자동 재시도. 다른 예외는 그대로 전파."""
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


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ── 데이터 클래스 ──────────────────────────────────────────────
@dataclass
class DailyBar:
    date: str
    close: float
    volume: int


@dataclass
class ScanRow:
    """단일 종목의 발굴 지표 일체."""
    ticker: str
    name: str
    current: int          # 당일 종가 (현재가)
    high_52w: int         # 52주(250일) 최고가
    pct_of_high: float    # 현재가 / 52주고
    today_volume: int     # 당일 거래량
    avg20_volume: float   # 직전 20거래일 평균 거래량
    vol_surge: float      # today_volume / avg20_volume
    ret_20d: float        # 종목 20거래일 수익률
    rs_score: float       # ret_20d - index_ret_20d


@dataclass
class SizingPlan:
    total_asset: int
    one_r: float
    buy_amount: float
    empty_slots: int


@dataclass
class OrderResult:
    ticker: str
    name: str
    qty: int = 0
    accepted: bool = False
    order_no: str = ""
    order_time: str = ""
    error: str = ""


@dataclass
class FillResult:
    ticker: str
    name: str
    qty_ordered: int
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
    for page in range(1, 23):
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


# ── 일봉 조회 (inquire-daily-itemchartprice 직호출) ──────────────
def _date_range_for(trading_days: int) -> tuple[str, str]:
    """trading_days 거래일을 안정적으로 확보할 수 있는 (시작, 종료) YYYYMMDD."""
    today = datetime.now()
    # 주말·공휴일 마진 ~1.6배 + 7일 여유 → 약 50거래일 확보
    span = int(max(trading_days, 1) * 1.6) + 7
    start = today - timedelta(days=span)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def fetch_chart(api: Api, code: str, trading_days: int,
                is_index: bool = False) -> tuple[dict, list[DailyBar]]:
    """inquire-daily-itemchartprice 호출.

    종목(is_index=False): output1 → 요약(stck_prpr, d250_hgpr 등),
                         output2 → 일봉 (stck_clpr / acml_vol).
    지수(is_index=True) : output2 의 종가는 bstp_nmix_prpr.

    반환되는 DailyBar 리스트는 과거 → 최신 순으로 정렬된다.
    """
    start, end = _date_range_for(trading_days)
    params = {
        "FID_COND_MRKT_DIV_CODE": "U" if is_index else "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
    }
    if not is_index:
        params["FID_ORG_ADJ_PRC"] = "0"
    req = APIRequestParameter(
        url_path=INDEX_CHART_URL_PATH if is_index else CHART_URL_PATH,
        tr_id=INDEX_CHART_TR_ID if is_index else CHART_TR_ID,
        params=params,
    )
    resp = api._send_get_request(req, raise_flag=False)
    if not resp.is_ok():
        raise RuntimeError(
            f"inquire-daily-itemchartprice 실패 ({code}): "
            f"rt_cd={resp.return_code} msg={resp.message}"
        )

    summary = resp.outputs[0] if resp.outputs else {}
    raw_bars = resp.outputs[1] if len(resp.outputs) >= 2 else []

    bars: list[DailyBar] = []
    for row in raw_bars:
        date = (row.get("stck_bsop_date") or "").strip()
        if not date:
            continue
        close = _to_float(
            row.get("bstp_nmix_prpr") if is_index else row.get("stck_clpr")
        )
        if close <= 0:
            continue
        volume = int(_to_float(row.get("acml_vol")))
        bars.append(DailyBar(date=date, close=close, volume=volume))

    bars.sort(key=lambda b: b.date)  # 과거 → 최신
    return summary, bars


def compute_index_return(api: Api) -> float:
    """KOSPI 200 의 최근 20거래일 수익률."""
    _, bars = call_with_retry(
        fetch_chart, api, KOSPI200_INDEX_CODE, INDEX_BARS_TRADING_DAYS,
        is_index=True, label="kospi200",
    )
    if len(bars) <= RS_LOOKBACK:
        raise RuntimeError(
            f"KOSPI200 일봉 데이터 부족 (받은 {len(bars)}건, 필요 {RS_LOOKBACK + 1}건)"
        )
    end_close = bars[-1].close
    start_close = bars[-(RS_LOOKBACK + 1)].close
    if start_close <= 0:
        raise RuntimeError("KOSPI200 기준일 종가가 0 이하")
    return end_close / start_close - 1.0


# ── 종목 스캔 ─────────────────────────────────────────────────
def scan_market(api: Api, tickers: list[tuple[str, str]],
                index_ret_20d: float) -> list[ScanRow]:
    """전 종목에 대해 현재가 시세 + 일봉을 받아와 ScanRow 로 변환.

    종목당 2회 호출:
      ① inquire-price  → 현재가(stck_prpr) + 52주 최고가(d250_hgpr)
      ② inquire-daily-itemchartprice → 일봉(거래량/종가)
    각 호출 사이에 SCAN_SLEEP 간격을 두어 모의투자 초당 2건 제한을 준수.
    """
    rows: list[ScanRow] = []
    total = len(tickers)
    for i, (ticker, name) in enumerate(tickers, 1):
        try:
            quote = call_with_retry(
                api._get_kr_stock_current_price_info, ticker,
                label=f"price {ticker}",
            )
            current = int(_to_float(quote.get("stck_prpr")))
            high_52w = int(_to_float(quote.get("d250_hgpr")))
            if current <= 0 or high_52w <= 0:
                time.sleep(SCAN_SLEEP)
                continue

            time.sleep(SCAN_SLEEP)

            _, bars = call_with_retry(
                fetch_chart, api, ticker, DAILY_BARS_TRADING_DAYS,
                is_index=False, label=ticker,
            )
            if len(bars) <= RS_LOOKBACK:
                continue  # 데이터 부족

            today_bar = bars[-1]
            prior_20 = bars[-(RS_LOOKBACK + 1):-1]   # 당일 제외 직전 20거래일
            if len(prior_20) != RS_LOOKBACK:
                continue

            avg20_vol = sum(b.volume for b in prior_20) / RS_LOOKBACK
            close_20d_ago = bars[-(RS_LOOKBACK + 1)].close
            if close_20d_ago <= 0:
                continue
            ret_20d = today_bar.close / close_20d_ago - 1.0

            rows.append(ScanRow(
                ticker=ticker,
                name=name,
                current=current,
                high_52w=high_52w,
                pct_of_high=current / high_52w,
                today_volume=today_bar.volume,
                avg20_volume=avg20_vol,
                vol_surge=(today_bar.volume / avg20_vol) if avg20_vol > 0
                          else 0.0,
                ret_20d=ret_20d,
                rs_score=ret_20d - index_ret_20d,
            ))
        except Exception as exc:
            print(f"  ! 조회 실패 {ticker} {name}: {exc}")
        if i % 30 == 0:
            print(f"      ... {i}/{total} 진행 (수집 {len(rows)})")
        time.sleep(SCAN_SLEEP)
    return rows


# ── 3대 필터 ──────────────────────────────────────────────────
def apply_filters(rows: list[ScanRow]) -> list[ScanRow]:
    """① 가격(52주 신고가 돌파) + ② 수급 통과 종목 중 ③ RS 상위 TOP_N_BY_RS 선정."""
    qualified = [
        r for r in rows
        if r.current >= r.high_52w
        and r.vol_surge >= VOLUME_SURGE_RATIO
    ]
    qualified.sort(key=lambda r: r.rs_score, reverse=True)
    return qualified[:TOP_N_BY_RS]


# ── 자산 / 잔고 ───────────────────────────────────────────────
def get_total_asset(api: Api) -> int:
    """총자산 = 예수금 + 주식평가금 = tot_evlu_amt."""
    res = call_with_retry(api._get_kr_total_balance, label="total_balance")
    if not res.outputs or len(res.outputs) < 2 or not res.outputs[1]:
        raise RuntimeError("잔고 응답에서 요약 영역(outputs[1])을 찾지 못했습니다")
    summary = res.outputs[1][0]
    return int(_to_float(summary.get("tot_evlu_amt")))


def snapshot_holdings(api: Api) -> dict[str, dict]:
    """{종목코드: {qty, avg_price, name}} 형태의 보유 종목 스냅샷."""
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


# ── 포지션 사이징 ─────────────────────────────────────────────
def build_sizing_plan(total_asset: int, current_holdings: int) -> SizingPlan:
    one_r = total_asset * ONE_R_RATIO
    buy_amount = one_r / STOP_LOSS_PCT
    empty_slots = max(MAX_POSITIONS - current_holdings, 0)
    return SizingPlan(
        total_asset=total_asset,
        one_r=one_r,
        buy_amount=buy_amount,
        empty_slots=empty_slots,
    )


# ── 매수 실행 ─────────────────────────────────────────────────
def execute_buys(api: Api, picks: list[ScanRow],
                 buy_amount: float) -> list[OrderResult]:
    """picks 를 위에서부터 시장가로 BUY_AMOUNT 만큼 매수."""
    results: list[OrderResult] = []
    for i, p in enumerate(picks, 1):
        qty = int(buy_amount // p.current) if p.current > 0 else 0
        result = OrderResult(ticker=p.ticker, name=p.name, qty=qty)
        print(f"  [{i}/{len(picks)}] {p.ticker} {p.name}"
              f" — 현재가 {p.current:,}, BUY_AMOUNT {buy_amount:,.0f},"
              f" 수량 {qty}주")
        if qty <= 0:
            result.error = "수량이 0 (현재가 > BUY_AMOUNT)"
            print(f"      → 스킵: {result.error}")
            results.append(result)
            time.sleep(ORDER_SLEEP)
            continue
        try:
            # price=0 → pykis 가 ORD_DVSN=01(시장가) 로 변환
            resp = call_with_retry(
                api.buy_kr_stock, p.ticker, qty, 0,
                label=f"buy {p.ticker}",
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
        time.sleep(ORDER_SLEEP)
    return results


# ── 체결 검증 ─────────────────────────────────────────────────
def verify_fills(api: Api, picks: list[ScanRow],
                 orders: list[OrderResult],
                 holdings_before: dict[str, dict]) -> list[FillResult]:
    """잔고 재조회 후 보유수량 델타로 체결 여부 판정."""
    holdings_after = snapshot_holdings(api)
    fills: list[FillResult] = []
    for p, o in zip(picks, orders):
        before = holdings_before.get(p.ticker, {"qty": 0})
        after = holdings_after.get(p.ticker, {"qty": 0, "avg_price": 0.0})
        fr = FillResult(
            ticker=p.ticker,
            name=p.name,
            qty_ordered=o.qty,
            qty_before=int(before["qty"]),
            qty_after=int(after["qty"]),
            avg_price_after=float(after.get("avg_price", 0.0)),
        )
        fr.filled = o.accepted and o.qty > 0 and fr.delta >= o.qty
        fills.append(fr)
    return fills


# ── 로깅 ─────────────────────────────────────────────────────
def log_filter_results(rows: list[ScanRow], picks: list[ScanRow]) -> None:
    qualified_count = sum(
        1 for r in rows
        if r.current >= r.high_52w
        and r.vol_surge >= VOLUME_SURGE_RATIO
    )
    print(f"      ①가격(52주 신고가 돌파) + ②수급"
          f"(≥{VOLUME_SURGE_RATIO:.0f}x) 통과: {qualified_count}개"
          f" → ③RS 상위 {len(picks)}개 선정")
    if not picks:
        return
    print(f"      {'순위':<4}{'종목코드':<10}{'종목명':<14}"
          f"{'현재가':>11}{'52주고%':>9}{'거래량x':>9}"
          f"{'20일수익':>10}{'RS점수':>10}")
    for rank, r in enumerate(picks, 1):
        print(f"      {rank:<4}{r.ticker:<10}{r.name:<14}"
              f"{r.current:>11,}{r.pct_of_high*100:>8.2f}%"
              f"{r.vol_surge:>8.2f}x{r.ret_20d*100:>9.2f}%"
              f"{r.rs_score*100:>9.2f}%")


def log_sizing(plan: SizingPlan, holdings_count: int) -> None:
    print(f"      총자산(TOTAL_ASSET)     : {plan.total_asset:>15,} 원")
    print(f"      1R (총자산×{ONE_R_RATIO*100:.1f}%)   : "
          f"{plan.one_r:>15,.0f} 원")
    print(f"      BUY_AMOUNT (1R/{STOP_LOSS_PCT*100:.0f}%)  : "
          f"{plan.buy_amount:>15,.0f} 원")
    print(f"      현재 보유 종목 수        : {holdings_count} / {MAX_POSITIONS}")
    print(f"      빈 슬롯 (신규 매수 한도) : {plan.empty_slots}")


def log_fill_summary(fills: list[FillResult],
                     orders: list[OrderResult]) -> None:
    """매수 체결 검증 결과를 모바일 세로형 카드 + 불릿 리스트로 출력."""
    sep = "━" * 32
    print()
    print(sep)
    print("🎯 매수 체결 검증 결과")
    print(sep)

    for idx, (f, o) in enumerate(zip(fills, orders), 1):
        if not o.accepted:
            status_emoji = "⚠️"
            status_text = "접수실패" if o.error else "스킵"
        elif f.filled:
            status_emoji = "🔴"
            status_text = "체결완료(매수)"
        else:
            status_emoji = "🛡️"
            status_text = "미체결(대기)"

        if f.delta > 0:
            delta_emoji = "🔴"
        elif f.delta < 0:
            delta_emoji = "🔵"
        else:
            delta_emoji = "▫️"

        print()
        print(f"[{idx}] {f.ticker} {f.name}")
        print(f"  • 주문번호: {o.order_no or '-'}")
        print(f"  • 주문수량: {f.qty_ordered:,} 주")
        print(f"  • 이전수량: {f.qty_before:,} 주")
        print(f"  • 현재수량: {f.qty_after:,} 주")
        print(f"  • 델타:    {delta_emoji} {f.delta:+,} 주")
        print(f"  • 평균단가: {f.avg_price_after:,.0f} 원")
        print(f"  • 결과:    {status_emoji} {status_text}")

    print()
    print(sep)
    filled_count = sum(1 for f in fills if f.filled)
    print(f"✅ 체결 완료: {filled_count}/{len(fills)} 종목")

    accepted_but_not_filled = [
        (f, o) for f, o in zip(fills, orders) if o.accepted and not f.filled
    ]
    if accepted_but_not_filled:
        print()
        print("📌 접수됐으나 체결 미확인 (장 마감/대기):")
        for f, o in accepted_but_not_filled:
            print(f"  • {f.ticker} {f.name} (주문 {o.order_no})")

    failed = [(f, o) for f, o in zip(fills, orders)
              if not o.accepted and o.error]
    if failed:
        print()
        print("⚠️ 접수 실패:")
        for f, o in failed:
            print(f"  • {f.ticker} {f.name}: {o.error}")


# ── 메인 ─────────────────────────────────────────────────────
def main() -> None:
    api, cfg = build_api()
    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(f"전략 파라미터: 1R={ONE_R_RATIO*100:.1f}%,"
          f" 손절폭={STOP_LOSS_PCT*100:.0f}%,"
          f" 최대보유={MAX_POSITIONS}, RS_TOP={TOP_N_BY_RS}")
    print(f"인프라 파라미터: scan_sleep={SCAN_SLEEP}s,"
          f" order_sleep={ORDER_SLEEP}s,"
          f" settlement_wait={SETTLEMENT_WAIT}s")

    print()
    print("[1/6] KOSPI 200 종목 목록 수집")
    tickers = fetch_kospi200_tickers()
    print(f"      → {len(tickers)}개 종목 확보")

    print()
    print(f"[2/6] KOSPI 200 지수 {RS_LOOKBACK}거래일 수익률 (RS 기준선)")
    index_ret = compute_index_return(api)
    print(f"      → KOSPI200 {RS_LOOKBACK}일 수익률: {index_ret*100:+.2f}%")
    time.sleep(SCAN_SLEEP)

    print()
    print("[3/6] 종목별 일봉 스캔 — 현재가/52주고/거래량/20일 수익률")
    rows = scan_market(api, tickers, index_ret)
    print(f"      → 분석 가능 종목 {len(rows)}개")

    picks = apply_filters(rows)
    log_filter_results(rows, picks)

    if not picks:
        print("\n3대 필터를 모두 통과한 종목이 없어 종료합니다.")
        return

    print()
    print("[4/6] 매수 직전 총자산 측정 + R-Multiple 포지션 사이징")
    time.sleep(ORDER_SLEEP)
    total_asset = get_total_asset(api)
    time.sleep(ORDER_SLEEP)
    holdings_before = snapshot_holdings(api)
    plan = build_sizing_plan(total_asset, len(holdings_before))
    log_sizing(plan, len(holdings_before))

    if plan.empty_slots <= 0:
        print(f"\n보유 종목이 이미 {len(holdings_before)}개"
              f"(≥{MAX_POSITIONS})라 신규 매수 차단. 종료합니다.")
        return

    final_targets = picks[:plan.empty_slots]
    print()
    print(f"[5/6] 시장가(종가 베팅) 매수"
          f" — 빈 슬롯 {plan.empty_slots}개,"
          f" 매수 대상 {len(final_targets)}개")
    time.sleep(ORDER_SLEEP)
    orders = execute_buys(api, final_targets, plan.buy_amount)

    print()
    print(f"[6/6] 체결 대기 {SETTLEMENT_WAIT}초 후 잔고 재조회")
    time.sleep(SETTLEMENT_WAIT)
    fills = verify_fills(api, final_targets, orders, holdings_before)
    with tee_capture() as buf:
        log_fill_summary(fills, orders)
    send_message(
        buf.getvalue(),
        title=f"[{mode}] PEAK-Easy R 매수 체결 검증",
    )


if __name__ == "__main__":
    main()

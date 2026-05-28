"""peakeasy_r_bot.py — 15:18 KST 1회 실행되는 PEAK Easy R-Model 매수 봇.

전략 흐름(원형 유지, 인프라만 다이어트):
    1. KOSPI 200 종목 리스트 수집 (네이버 KPI200 스크레이핑)
    2. KOSPI 200 지수의 RS_LOOKBACK(20거래일) 수익률 산출 (RS 기준선)
    3. 종목별 일봉(40거래일) + 현재가 시세 수집
    4. 3대 필터:
       ① 가격: 현재가 ≥ 52주(250일) 최고가  (Breakout)
       ② 수급: 당일 거래량 ≥ 직전 20일 평균 거래량 × 2.0
       ③ 강도: (종목 20일 수익률 − KOSPI200 20일 수익률) 상위 5개
    5. 매수 직전 총자산 + 보유 슬롯 확인 → R-Multiple 사이징
       1R = TOTAL_ASSET × 1.6%
       BUY_AMOUNT = 1R / 8%  ( = TOTAL_ASSET × 20%)
    6. 시장가(종가 베팅) 매수 — 수량 = BUY_AMOUNT // 현재가
    7. 3초 대기 후 잔고 델타로 체결 검증, 모바일 세로형 카드 리포트 전송
    8. 미체결 잔여 수량은 unfilled_orders.json 의 BUY 버킷에 기록
       → 다음 영업일 아침 morning_bot 이 기어코 체결시킨다.

본 스크립트는 무한 루프/스케줄러 없는 싱글샷. crontab 으로 15:18 KST 1회 실행.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from pykis import Api
from pykis.public_api import APIRequestParameter

from shared_utils import (
    MAX_POSITIONS,
    ONE_R_RATIO,
    RATE_LIMIT_SLEEP,
    SEP_LINE,
    SETTLEMENT_WAIT,
    STOP_LOSS_RATIO,
    STRATEGY_NAME,
    UNFILLED_MEMO_FILE,
    FillResult,
    OrderResult,
    OrderTarget,
    apply_fill_results_to_memo,
    build_api,
    build_fill_results,
    fetch_account_summary,
    get_total_asset,
    load_unfilled_memo,
    log_fill_card,
    place_orders,
    print_account_summary_panel,
    save_unfilled_memo,
    snapshot_holdings_dict,
    _now_kst_str,
)
from telegram_notifier import send_message, tee_capture


# ── 전략 파라미터 (R-Multiple) ─────────────────────────────────
VOLUME_SURGE_RATIO = 2.0
RS_LOOKBACK = 20
TOP_N_BY_RS = 5

# ── 데이터 수집 파라미터 ───────────────────────────────────────
DAILY_BARS_TRADING_DAYS = 40
INDEX_BARS_TRADING_DAYS = 20
KOSPI200_INDEX_CODE = "2001"

# ── 인프라 ────────────────────────────────────────────────────
# 종목 스캔(조회)용 sleep — 1차 실패율을 0%에 가깝게 만들어 패자부활 의존도 최소화.
# 주문(place_orders)에는 영향 없음(shared_utils._place_with_retry 의 2/4/8s 백오프 그대로 유지).
# 0.1s→47% 실패, 0.5s→12% 실패, 1.0s→~0% 실패 (2026-05-28 실측, KIS 모의투자)
SCAN_SLEEP = 1.0
# 패자부활전(Sweep) — 1차 스캔에서 실패한 종목들을 다음 영업일로 넘기지 않고 즉시 재시도
MAX_SWEEP_ROUNDS = 2     # 최대 패자부활 회차
SWEEP_COOLDOWN = 6.0     # 각 부활전 직전 cooldown — KIS rate limit 윈도우 회복 시간
NAVER_URL = "https://finance.naver.com/sise/entryJongmok.naver"
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}

RATE_LIMIT_MARKERS = ("초당 거래건수", "초당 거래 건수")
MAX_RETRIES = 4
RETRY_BACKOFFS = (1.0, 2.0, 3.0, 5.0)

CHART_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
CHART_TR_ID = "FHKST03010100"
INDEX_CHART_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
INDEX_CHART_TR_ID = "FHKUP03500100"


# ── 데이터 클래스 ──────────────────────────────────────────────
@dataclass
class DailyBar:
    date: str
    close: float
    volume: int


@dataclass
class ScanRow:
    ticker: str
    name: str
    current: int
    high_52w: int
    pct_of_high: float
    today_volume: int
    avg20_volume: float
    vol_surge: float
    ret_20d: float
    rs_score: float


@dataclass
class SizingPlan:
    total_asset: int
    one_r: float
    buy_amount: float
    empty_slots: int


# ── 보조 ────────────────────────────────────────────────────────
def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(m in msg for m in RATE_LIMIT_MARKERS)


def call_with_retry(fn, *args, label: str = "", **kwargs):
    """KIS rate-limit 에 한해 자동 재시도. 다른 예외는 그대로 전파."""
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
            print(
                f"  ↻ {tag}rate limit, {backoff}s 대기 후 재시도"
                f" ({attempt + 1}/{MAX_RETRIES})"
            )
            time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ── KOSPI 200 종목 리스트 ───────────────────────────────────────
def fetch_kospi200_tickers() -> list[tuple[str, str]]:
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
            break
    return list(pairs.items())


# ── 일봉 조회 ───────────────────────────────────────────────────
def _date_range_for(trading_days: int) -> tuple[str, str]:
    today = datetime.now()
    span = int(max(trading_days, 1) * 1.6) + 7
    start = today - timedelta(days=span)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def fetch_chart(
    api: Api, code: str, trading_days: int, is_index: bool = False
) -> tuple[dict, list[DailyBar]]:
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
        d = (row.get("stck_bsop_date") or "").strip()
        if not d:
            continue
        close = _to_float(
            row.get("bstp_nmix_prpr") if is_index else row.get("stck_clpr")
        )
        if close <= 0:
            continue
        volume = int(_to_float(row.get("acml_vol")))
        bars.append(DailyBar(date=d, close=close, volume=volume))
    bars.sort(key=lambda b: b.date)
    return summary, bars


def compute_index_return(api: Api) -> float:
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
def _scan_one_ticker(
    api: Api, ticker: str, name: str, index_ret_20d: float
) -> ScanRow | None:
    """단일 종목 스캔. 조건 미달은 None 반환, API 예외는 그대로 raise.

    API 예외는 호출측(scan_market)이 잡아서 패자부활 큐로 보낸다.
    """
    quote = api._get_kr_stock_current_price_info(ticker)
    current = int(_to_float(quote.get("stck_prpr")))
    high_52w = int(_to_float(quote.get("d250_hgpr")))
    if current <= 0 or high_52w <= 0:
        return None

    time.sleep(SCAN_SLEEP)

    _, bars = fetch_chart(api, ticker, DAILY_BARS_TRADING_DAYS, is_index=False)
    if len(bars) <= RS_LOOKBACK:
        return None

    today_bar = bars[-1]
    prior_20 = bars[-(RS_LOOKBACK + 1):-1]
    if len(prior_20) != RS_LOOKBACK:
        return None

    avg20_vol = sum(b.volume for b in prior_20) / RS_LOOKBACK
    close_20d_ago = bars[-(RS_LOOKBACK + 1)].close
    if close_20d_ago <= 0:
        return None
    ret_20d = today_bar.close / close_20d_ago - 1.0

    return ScanRow(
        ticker=ticker,
        name=name,
        current=current,
        high_52w=high_52w,
        pct_of_high=current / high_52w,
        today_volume=today_bar.volume,
        avg20_volume=avg20_vol,
        vol_surge=(today_bar.volume / avg20_vol) if avg20_vol > 0 else 0.0,
        ret_20d=ret_20d,
        rs_score=ret_20d - index_ret_20d,
    )


def scan_market(
    api: Api, tickers: list[tuple[str, str]], index_ret_20d: float
) -> list[ScanRow]:
    """1차 빠른 스캔 + 패자부활전(최대 MAX_SWEEP_ROUNDS회) 으로 누락을 제로화."""
    rows: list[ScanRow] = []
    failed: list[tuple[str, str]] = []
    total = len(tickers)

    # ── 1차 스캔: 빠르게 한 바퀴, 실패 종목은 retry 큐로 ──
    for i, (ticker, name) in enumerate(tickers, 1):
        try:
            row = _scan_one_ticker(api, ticker, name, index_ret_20d)
            if row is not None:
                rows.append(row)
        except Exception as exc:
            print(f"  ! 1차 조회 실패 {ticker} {name}: {exc} → 패자부활 큐")
            failed.append((ticker, name))
        if i % 30 == 0:
            print(
                f"      ... {i}/{total} 진행"
                f" (수집 {len(rows)}, 실패 {len(failed)})"
            )
        time.sleep(SCAN_SLEEP)

    # ── 패자부활전: 실패 큐가 빌 때까지 최대 MAX_SWEEP_ROUNDS 회 ──
    for sweep_round in range(1, MAX_SWEEP_ROUNDS + 1):
        if not failed:
            break
        print(
            f"  🔁 패자부활전 {sweep_round}/{MAX_SWEEP_ROUNDS} —"
            f" {len(failed)}종목 재시도 (cooldown {SWEEP_COOLDOWN}s)"
        )
        time.sleep(SWEEP_COOLDOWN)
        still_failed: list[tuple[str, str]] = []
        for ticker, name in failed:
            try:
                row = _scan_one_ticker(api, ticker, name, index_ret_20d)
                if row is not None:
                    rows.append(row)
            except Exception as exc:
                print(f"    ! 부활 {sweep_round}차 실패 {ticker} {name}: {exc}")
                still_failed.append((ticker, name))
            time.sleep(SCAN_SLEEP)
        recovered = len(failed) - len(still_failed)
        print(
            f"      → 부활 {sweep_round}차 결과: 복구 {recovered}종목,"
            f" 잔여 실패 {len(still_failed)}종목"
        )
        failed = still_failed

    if failed:
        print(
            f"  ⚠️ 패자부활전 후에도 누락 {len(failed)}종목 — 종목코드: "
            f"{[t for t, _ in failed]}"
        )
    else:
        print(f"  ✅ 누락 없음 — 전 종목 스캔 완료")
    return rows


# ── 3대 필터 ──────────────────────────────────────────────────
def apply_filters(rows: list[ScanRow]) -> list[ScanRow]:
    qualified = [
        r for r in rows
        if r.current >= r.high_52w and r.vol_surge >= VOLUME_SURGE_RATIO
    ]
    qualified.sort(key=lambda r: r.rs_score, reverse=True)
    return qualified[:TOP_N_BY_RS]


# ── R-Multiple 사이징 ─────────────────────────────────────────
def build_sizing_plan(total_asset: int, current_holdings: int) -> SizingPlan:
    one_r = total_asset * ONE_R_RATIO
    buy_amount = one_r / STOP_LOSS_RATIO
    empty_slots = max(MAX_POSITIONS - current_holdings, 0)
    return SizingPlan(
        total_asset=total_asset,
        one_r=one_r,
        buy_amount=buy_amount,
        empty_slots=empty_slots,
    )


# ── 로깅 ─────────────────────────────────────────────────────
def log_filter_results(rows: list[ScanRow], picks: list[ScanRow]) -> None:
    qualified_count = sum(
        1 for r in rows
        if r.current >= r.high_52w and r.vol_surge >= VOLUME_SURGE_RATIO
    )
    print(
        f"      ①가격(52주 신고가 돌파) + ②수급(≥{VOLUME_SURGE_RATIO:.0f}x)"
        f" 통과: {qualified_count}개 → ③RS 상위 {len(picks)}개 선정"
    )
    if not picks:
        return
    print(
        f"      {'순위':<4}{'종목코드':<10}{'종목명':<14}{'현재가':>11}"
        f"{'52주고%':>9}{'거래량x':>9}{'20일수익':>10}{'RS점수':>10}"
    )
    for rank, r in enumerate(picks, 1):
        print(
            f"      {rank:<4}{r.ticker:<10}{r.name:<14}{r.current:>11,}"
            f"{r.pct_of_high*100:>8.2f}%{r.vol_surge:>8.2f}x"
            f"{r.ret_20d*100:>9.2f}%{r.rs_score*100:>9.2f}%"
        )


def log_sizing_panel(plan: SizingPlan, holdings_count: int) -> None:
    print(SEP_LINE)
    print("📐 R-Multiple 포지션 사이징")
    print(SEP_LINE)
    print(f"• 총자산(TOTAL_ASSET):  {plan.total_asset:>15,} 원")
    print(f"• 1R (총자산×{ONE_R_RATIO*100:.1f}%):    {plan.one_r:>15,.0f} 원")
    print(f"• BUY_AMOUNT (1R/{STOP_LOSS_RATIO*100:.0f}%):  {plan.buy_amount:>15,.0f} 원")
    print(f"• 현재 보유 종목 수:    {holdings_count} / {MAX_POSITIONS}")
    print(f"• 빈 슬롯 (매수 한도):  {plan.empty_slots}")


def log_picks_panel(picks: list[ScanRow]) -> None:
    print(SEP_LINE)
    print(f"🎯 3대 필터 선정 종목 (TOP {len(picks)})")
    print(SEP_LINE)
    if not picks:
        print("(선정 종목 없음)")
        return
    for idx, r in enumerate(picks, 1):
        print()
        print(f"[{idx}] {r.ticker} {r.name}")
        print(f"  • 현재가:      {r.current:>12,} 원")
        print(f"  • 52주 최고가: {r.high_52w:>12,} 원 ({r.pct_of_high*100:.2f}%)")
        print(f"  • 거래량:      {r.today_volume:>12,} 주")
        print(f"  • 20일 평균:   {r.avg20_volume:>12,.0f} 주")
        print(f"  • 거래량 배수: {r.vol_surge:>11.2f}x")
        print(f"  • 20일 수익률: {r.ret_20d*100:>+11.2f}%")
        print(f"  • RS 점수:     {r.rs_score*100:>+11.2f}%")


def log_buy_report(
    fills: list[FillResult],
    orders: list[OrderResult],
    fully: list[FillResult],
    partially: list[FillResult],
) -> None:
    print(SEP_LINE)
    print("🎯 매수 체결 검증 결과")
    print(SEP_LINE)
    if not fills:
        print("(주문 없음)")
        return
    for idx, (f, o) in enumerate(zip(fills, orders), 1):
        log_fill_card(f, o, idx)
    print()
    print(SEP_LINE)
    print(f"✅ 체결 완료: {len(fully)}/{len(fills)} 종목")
    if partially:
        print()
        print("📌 미체결 → BUY 장부 이월 (다음 영업일 아침 0순위 재매수):")
        for f in partially:
            print(f"  • {f.ticker} {f.name} 잔여 {f.unfilled_qty:,}주")


# ── 메인 ─────────────────────────────────────────────────────
def main() -> None:
    api, cfg = build_api()
    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(
        f"전략 파라미터: 1R={ONE_R_RATIO*100:.1f}%,"
        f" 손절폭={STOP_LOSS_RATIO*100:.0f}%,"
        f" 최대보유={MAX_POSITIONS}, RS_TOP={TOP_N_BY_RS}"
    )
    print(
        f"인프라 파라미터: scan_sleep={SCAN_SLEEP}s,"
        f" rate_sleep={RATE_LIMIT_SLEEP}s,"
        f" settlement_wait={SETTLEMENT_WAIT}s"
    )
    print(f"미체결 메모 파일: {UNFILLED_MEMO_FILE}")

    memo = load_unfilled_memo()

    # ── [1/6] KOSPI 200 종목 목록 ────────────────────────────────
    print()
    print("[1/6] KOSPI 200 종목 목록 수집")
    tickers = fetch_kospi200_tickers()
    print(f"      → {len(tickers)}개 종목 확보")

    # ── [2/6] KOSPI 200 RS 기준선 ────────────────────────────────
    print()
    print(f"[2/6] KOSPI 200 지수 {RS_LOOKBACK}거래일 수익률 (RS 기준선)")
    index_ret = compute_index_return(api)
    print(f"      → KOSPI200 {RS_LOOKBACK}일 수익률: {index_ret*100:+.2f}%")
    time.sleep(SCAN_SLEEP)

    # ── [3/6] 종목 스캔 + 필터 ───────────────────────────────────
    print()
    print("[3/6] 종목별 일봉 스캔 — 현재가/52주고/거래량/20일 수익률")
    rows = scan_market(api, tickers, index_ret)
    print(f"      → 분석 가능 종목 {len(rows)}개")
    picks = apply_filters(rows)
    log_filter_results(rows, picks)

    # 리포트 청크 — 마지막에 텔레그램 1통으로 모아 전송
    rep_chunks: list[str] = []
    rep_title = f"[{mode}] [{STRATEGY_NAME}] PEAK-Easy R 매수 결과 리포트"
    with tee_capture() as buf:
        print(f"🚀 [{STRATEGY_NAME}] PEAK-Easy R 매수 결과 리포트")
        print(f"🕐 {_now_kst_str()}")
        print()
        log_picks_panel(picks)
    rep_chunks.append(buf.getvalue())

    if not picks:
        with tee_capture() as buf:
            print()
            time.sleep(3.0)  # KIS rate limit 윈도우 회복 후 계좌 요약 조회
            print_account_summary_panel(fetch_account_summary(api), title="💰 매수 후 계좌 요약")
        rep_chunks.append(buf.getvalue())
        send_message("".join(rep_chunks), title=rep_title)
        save_unfilled_memo(memo)
        print("\n3대 필터를 모두 통과한 종목이 없어 종료합니다.")
        return

    # ── [4/6] 총자산 + 사이징 ────────────────────────────────────
    print()
    print("[4/6] 매수 직전 총자산 측정 + R-Multiple 포지션 사이징")
    time.sleep(RATE_LIMIT_SLEEP)
    total_asset = get_total_asset(api)
    time.sleep(RATE_LIMIT_SLEEP)
    holdings_before = snapshot_holdings_dict(api)
    plan = build_sizing_plan(total_asset, len(holdings_before))
    with tee_capture() as buf:
        print()
        log_sizing_panel(plan, len(holdings_before))
    rep_chunks.append(buf.getvalue())

    if plan.empty_slots <= 0:
        with tee_capture() as buf:
            print()
            print(SEP_LINE)
            print(
                f"⚠️ 보유 종목 {len(holdings_before)}개(≥{MAX_POSITIONS})"
                f" — 신규 매수 차단"
            )
            print(SEP_LINE)
            print()
            time.sleep(3.0)  # KIS rate limit 윈도우 회복 후 계좌 요약 조회
            print_account_summary_panel(fetch_account_summary(api), title="💰 매수 후 계좌 요약")
        rep_chunks.append(buf.getvalue())
        send_message("".join(rep_chunks), title=rep_title)
        save_unfilled_memo(memo)
        print(
            f"\n보유 종목이 이미 {len(holdings_before)}개"
            f"(≥{MAX_POSITIONS})라 신규 매수 차단. 종료합니다."
        )
        return

    final_targets = picks[:plan.empty_slots]

    # ── [5/6] 시장가 매수 ────────────────────────────────────────
    print()
    print(
        f"[5/6] 시장가(종가 베팅) 매수 — 빈 슬롯 {plan.empty_slots}개,"
        f" 매수 대상 {len(final_targets)}개"
    )
    qty_before = {t: int(d["qty"]) for t, d in holdings_before.items()}
    order_targets = []
    for p in final_targets:
        qty = int(plan.buy_amount // p.current) if p.current > 0 else 0
        order_targets.append(OrderTarget(side="BUY", ticker=p.ticker, name=p.name, qty=qty))
    time.sleep(RATE_LIMIT_SLEEP)
    orders = place_orders(api, order_targets)

    # ── [6/6] 체결 검증 + BUY 미체결 메모 ───────────────────────────
    print()
    print(f"[6/6] 체결 대기 {SETTLEMENT_WAIT}초 후 잔고 재조회")
    time.sleep(SETTLEMENT_WAIT)
    df_after = snapshot_holdings_dict(api)
    qty_after = {t: int(d["qty"]) for t, d in df_after.items()}
    fills = build_fill_results(orders, qty_before, qty_after)
    fully, partially = apply_fill_results_to_memo(memo, fills, orders)

    with tee_capture() as buf:
        print()
        log_buy_report(fills, orders, fully, partially)
        print()
        time.sleep(3.0)  # KIS rate limit 윈도우 회복 후 계좌 요약 조회
        print_account_summary_panel(fetch_account_summary(api), title="💰 매수 후 계좌 요약")
    rep_chunks.append(buf.getvalue())
    send_message("".join(rep_chunks), title=rep_title)

    save_unfilled_memo(memo)
    print(
        f"\n장부 저장 완료 — BUY 잔존 {len(memo.get('BUY', {}))}건"
        f" (다음 영업일 아침 0순위 재시도)"
    )


if __name__ == "__main__":
    main()

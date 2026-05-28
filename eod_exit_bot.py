"""eod_exit_bot.py — 15:15 KST 1회 실행되는 종가 기준 청산 봇.

역할 분리(2026-05 개편):
    * 장중 실시간 손절은 intraday_stop_loss_bot.py 가 09:00~15:14 전담 (30초 폴링)
    * 본 봇은 (A) 20MA 추세이탈 청산 + (B) 손절 안전망 역할
      └ 안전망: 장중봇이 15:14에 종료된 직후 ~ 15:15 사이 새로 -8% 진입한 종목을
        EOD 시점에 1회 더 스캔하여 청산 (다중 방어선)

흐름:
    1. 보유 잔고 조회 → "장 마감 전 포트폴리오" 텔레그램 전송
    2. 보유 종목별 일봉 → 20MA 산출
    3. 청산 조건 평가
        A: 수익률 ≤ STOP_LOSS_PCT  (손절 안전망 — 통상 장중봇이 먼저 처리)
        B: 현재가 < 20MA            (추세이탈 — EOD 전담)
       하나라도 만족 → 청산 대상
    4. 시장가 매도 주문 (장중 시장가, 주문 간 RATE_LIMIT_SLEEP)
    5. SETTLEMENT_WAIT(3초) 대기 후 잔고 델타로 체결 검증
       - 완전 체결: 장부에서 정리 (이미 비어있음)
       - 미체결 잔여수량: unfilled_orders.json 의 SELL 버킷에 기록
         → 다음 영업일 아침 morning_bot 가 0순위로 재시도

본 스크립트는 무한 루프/스케줄러 없는 싱글샷. crontab 으로 15:15 KST 1회 실행 권장.
※ 전일 미체결 SELL/BUY 0순위 청산은 morning_bot 담당 — 본 스크립트에서는 처리하지 않음.
"""

from __future__ import annotations

import time

from pykis import Api

from shared_utils import (
    MA_WINDOW,
    RATE_LIMIT_SLEEP,
    SEP_LINE,
    SETTLEMENT_WAIT,
    STOP_LOSS_PCT,
    STRATEGY_NAME,
    UNFILLED_MEMO_FILE,
    ExitDecision,
    FillResult,
    Holding,
    OrderResult,
    OrderTarget,
    apply_fill_results_to_memo,
    build_api,
    build_fill_results,
    fetch_account_summary,
    fetch_holdings,
    load_unfilled_memo,
    log_fill_card,
    place_orders,
    print_dashboard,
    print_post_liquidation_panel,
    save_unfilled_memo,
    snapshot_holdings_dict,
    _now_kst_str,
    _profit_emoji,
    _query_qty_after,
)
from telegram_notifier import send_message, tee_capture


# ── 20MA 산출 & 청산 판단 ───────────────────────────────────────
def fetch_ma20(api: Api, ticker: str) -> float | None:
    df = api.get_kr_ohlcv(ticker, "D")
    if df is None or df.empty:
        return None
    df = df.sort_index()
    closes = df["Close"].tail(MA_WINDOW)
    if len(closes) < MA_WINDOW:
        return None
    return float(closes.mean())


def decide_exits(api: Api, holdings: list[Holding]) -> list[ExitDecision]:
    decisions: list[ExitDecision] = []
    for h in holdings:
        time.sleep(RATE_LIMIT_SLEEP)
        ma20 = fetch_ma20(api, h.ticker)
        decision = ExitDecision(holding=h, ma20=ma20)
        if h.profit_rate <= STOP_LOSS_PCT:
            # 안전망 — 통상 장중 손절봇이 이미 처리. 15:14~15:15 신규 진입분만 잡힘.
            decision.reasons.append(
                f"손절 안전망(수익률 {h.profit_rate:+.2f}% ≤ {STOP_LOSS_PCT:+.2f}%)"
            )
        if ma20 is None:
            print(
                f"  ! {h.ticker} {h.name}: 일봉 데이터 부족으로 20MA 산출 불가"
                f" — 조건 B 미적용"
            )
        elif h.current_price < ma20:
            decision.reasons.append(
                f"추세이탈(현재가 {h.current_price:,} < 20MA {ma20:,.1f})"
            )
        decisions.append(decision)
    return decisions


def log_decisions(decisions: list[ExitDecision]) -> list[ExitDecision]:
    print(SEP_LINE)
    print(f"🎯 종가 청산 판단 — {MA_WINDOW}MA 추세이탈 + 손절 안전망")
    print(SEP_LINE)
    exits: list[ExitDecision] = []
    for idx, d in enumerate(decisions, 1):
        h = d.holding
        if d.should_exit:
            verdict_emoji = "🔵"
            verdict_text = "청산"
        else:
            verdict_emoji = "🛡️"
            verdict_text = "보유(방어)"
        pr_emoji = _profit_emoji(h.profit_rate)
        amt_emoji = _profit_emoji(h.profit_amount)
        ma_str = f"{d.ma20:,.1f}" if d.ma20 is not None else "N/A"
        reason = " / ".join(d.reasons) if d.reasons else "조건 미충족"
        print()
        print(f"[{idx}] {h.ticker} {h.name}")
        print(f"  • 현재가:      {h.current_price:>12,} 원")
        print(f"  • {MA_WINDOW}MA:        {ma_str:>12} 원")
        print(f"  • 보유수량:    {h.qty:>12,} 주")
        print(f"  • 평가금액:    {h.eval_amount:>12,} 원")
        print(f"  • 종목 수익금: {amt_emoji} {h.profit_amount:>+11,} 원")
        print(f"  • 수익률:      {pr_emoji} {h.profit_rate:+.2f}%")
        print(f"  • 판단:        {verdict_emoji} {verdict_text}")
        print(f"  • 사유:        {reason}")
        if d.should_exit:
            exits.append(d)
    print()
    print(SEP_LINE)
    print(f"청산 대상: {len(exits)}/{len(decisions)} 종목")
    return exits


def log_fill_report(
    fills: list[FillResult],
    orders: list[OrderResult],
    fully: list[FillResult],
    partially: list[FillResult],
) -> None:
    print(SEP_LINE)
    print("🎯 청산 체결 검증 결과")
    print(SEP_LINE)
    for idx, (f, o) in enumerate(zip(fills, orders), 1):
        log_fill_card(f, o, idx)
    print()
    print(SEP_LINE)
    print(f"✅ 청산 완료: {len(fully)}/{len(fills)} 종목")
    if partially:
        print()
        print("📌 미체결 → SELL 장부 이월 (다음 영업일 아침 0순위 재청산):")
        for f in partially:
            print(f"  • {f.ticker} {f.name} 잔여 {f.unfilled_qty:,}주")


# ── 메인 ────────────────────────────────────────────────────────
def main() -> None:
    api, cfg = build_api()
    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(
        f"파라미터: stop_loss={STOP_LOSS_PCT}%, ma_window={MA_WINDOW}일, "
        f"rate_sleep={RATE_LIMIT_SLEEP}s, settlement_wait={SETTLEMENT_WAIT}s"
    )
    print(f"미체결 메모 파일: {UNFILLED_MEMO_FILE}")

    memo = load_unfilled_memo()

    # ── [1/4] 잔고 조회 & 장 마감 전 대시보드 ────────────────────────
    print()
    print("[1/4] 잔고 조회 & 장 마감 전 대시보드 출력")
    holdings = fetch_holdings(api)
    with tee_capture() as buf:
        print_dashboard(api, holdings, is_morning=False)
    send_message(
        buf.getvalue(),
        title=f"[{mode}] [{STRATEGY_NAME}] 장 마감 전 포트폴리오",
    )

    eod_chunks: list[str] = []
    eod_title = f"[{mode}] [{STRATEGY_NAME}] 종가 청산 결과 리포트"
    with tee_capture() as buf:
        print(f"🛡️ [{STRATEGY_NAME}] 종가 청산 결과 리포트")
        print(f"🕐 {_now_kst_str()}")
        print()
    eod_chunks.append(buf.getvalue())

    if not holdings:
        with tee_capture() as buf:
            print(SEP_LINE)
            print("ℹ️ 보유 종목 없음 — 청산 대상 없음")
            print(SEP_LINE)
            print()
            time.sleep(3.0)  # KIS rate limit 윈도우 회복 후 계좌 요약 조회
            print_post_liquidation_panel(fetch_account_summary(api))
        eod_chunks.append(buf.getvalue())
        send_message("".join(eod_chunks), title=eod_title)
        save_unfilled_memo(memo)
        print("\n보유 종목이 없어 종료합니다.")
        return

    # ── [2/4] 20MA & 청산 조건 평가 ──────────────────────────────────
    print()
    print("[2/4] 20일 이동평균 산출 & 청산 조건 판단")
    decisions = decide_exits(api, holdings)
    with tee_capture() as buf:
        exits = log_decisions(decisions)
    eod_chunks.append(buf.getvalue())

    if not exits:
        with tee_capture() as buf:
            print()
            time.sleep(3.0)  # KIS rate limit 윈도우 회복 후 계좌 요약 조회
            print_post_liquidation_panel(fetch_account_summary(api))
        eod_chunks.append(buf.getvalue())
        send_message("".join(eod_chunks), title=eod_title)
        save_unfilled_memo(memo)
        print("\n청산 대상 없음. 종료합니다.")
        return

    # ── [3/4] 시장가 매도 ────────────────────────────────────────
    print()
    print(f"[3/4] 시장가 매도 주문 ({len(exits)}종목)")
    holdings_map = snapshot_holdings_dict(api)
    qty_before = {t: int(d["qty"]) for t, d in holdings_map.items()}
    targets = [
        OrderTarget(side="SELL", ticker=d.holding.ticker, name=d.holding.name, qty=d.holding.qty)
        for d in exits
    ]
    orders = place_orders(api, targets)

    # ── [4/4] 체결 검증 & SELL 미체결 메모 업데이트 ──────────────────
    print()
    print(f"[4/4] 체결 대기 {SETTLEMENT_WAIT}초 후 잔고 재조회")
    time.sleep(SETTLEMENT_WAIT)
    qty_after = _query_qty_after(api)
    fills = build_fill_results(orders, qty_before, qty_after)
    fully, partially = apply_fill_results_to_memo(memo, fills, orders)

    with tee_capture() as buf:
        print()
        log_fill_report(fills, orders, fully, partially)
        print()
        time.sleep(3.0)  # KIS rate limit 윈도우 회복 후 계좌 요약 조회
        print_post_liquidation_panel(fetch_account_summary(api))
    eod_chunks.append(buf.getvalue())
    send_message("".join(eod_chunks), title=eod_title)

    save_unfilled_memo(memo)
    print(
        f"\n장부 저장 완료 — SELL 잔존 {len(memo.get('SELL', {}))}건"
        f" (다음 영업일 아침 0순위 재시도)"
    )


if __name__ == "__main__":
    main()

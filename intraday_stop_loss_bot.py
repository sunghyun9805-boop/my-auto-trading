"""intraday_stop_loss_bot.py — 장중 실시간 손절 전담 봇 (싱글샷 + 내부 루프).

역할 분리(2026-05 개편):
    * 본 봇: 09:00~15:14 KST 동안 INTRADAY_POLL_INTERVAL(30초) 주기로 잔고를
      폴링하여 수익률 ≤ STOP_LOSS_PCT(-8%) 종목을 즉시 시장가 청산.
    * EOD봇(eod_exit_bot.py): 15:15 KST 단발 — 20MA 추세이탈 청산 + 손절 안전망.
      └ 안전망: 본 봇이 15:14에 종료된 직후 ~ 15:15 사이 새로 -8% 진입한 종목을
        EOD 시점에 1회 더 스캔하여 청산.

내부 루프 채택 이유:
    * KIS 토큰은 1분 1회만 발급 가능 → 30초 cron 으로 매번 새 프로세스 띄우면
      토큰 발급 한도에 즉시 걸린다. 단일 프로세스 = 단일 토큰 6시간 재사용.
    * 단일 프로세스 = 동일 계정 동시 호출 위험(403/500) 원천 차단.
    * 프로세스 사망 대비는 외부 cron 워치독(15분)으로 메꿈.

흐름:
    1. 기동 텔레그램 1통 (모드/계좌/파라미터)
    2. 루프 (should_continue_intraday() == True):
        a. fetch_holdings() — 실패 시 한 사이클 스킵
        b. profit_rate ≤ STOP_LOSS_PCT AND ticker ∉ attempted_today 필터
        c. 손절 대상 있으면:
           - 잔고 스냅샷(qty_before)
           - place_orders(SELL 시장가)
           - SETTLEMENT_WAIT 대기 → 잔고 재조회 → 체결 검증
           - 미체결 잔여수량은 unfilled_orders.json SELL 버킷 이월 (다음날 아침봇)
           - attempted_today 에 ticker 마킹 (같은날 재주문 방지)
           - 텔레그램 카드 전송 (사이클당 1통)
        d. INTRADAY_POLL_INTERVAL 만큼 sleep
    3. 종료 텔레그램 1통 (총 손절 건수/잔존 미체결 요약)

운영 가정:
    * 영업일 09:00 KST cron 으로 기동.
    * 휴장일/장 미개시 시 KIS 잔고조회는 평소처럼 응답하지만 시세가 정지 → 손절
      조건이 트리거되지 않으므로 안전. (그래도 보호 차원에서 KIS 예외는 흡수)
    * 프로세스 사망 시 외부 cron 워치독이 15분 내 재기동 → 최대 노출 15분.
"""

from __future__ import annotations

import sys
import time
import traceback

from shared_utils import (
    INTRADAY_END_HMS,
    INTRADAY_POLL_INTERVAL,
    SEP_LINE,
    SETTLEMENT_WAIT,
    STOP_LOSS_PCT,
    STRATEGY_NAME,
    UNFILLED_MEMO_FILE,
    FillResult,
    Holding,
    OrderResult,
    OrderTarget,
    apply_fill_results_to_memo,
    build_api,
    build_fill_results,
    fetch_holdings,
    load_unfilled_memo,
    log_fill_card,
    place_orders,
    save_unfilled_memo,
    should_continue_intraday,
    _now_kst_str,
    _query_qty_after,
)
from telegram_notifier import send_message, tee_capture


# ── 손절 후보 추출 ─────────────────────────────────────────────
def pick_stop_loss_triggers(
    holdings: list[Holding], attempted_today: set[str]
) -> list[Holding]:
    """수익률 ≤ STOP_LOSS_PCT 이고 오늘 아직 손절 시도하지 않은 종목."""
    return [
        h for h in holdings
        if h.profit_rate <= STOP_LOSS_PCT and h.ticker not in attempted_today
    ]


# ── 손절 1회 처리 ─────────────────────────────────────────────
def handle_stop_loss(
    api, triggered: list[Holding], attempted_today: set[str], mode: str
) -> tuple[int, int]:
    """손절 대상 종목들을 시장가 매도 → 체결 검증 → 텔레그램 → 장부 갱신.

    Returns:
        (완전체결 종목수, 부분/미체결 종목수)
    """
    memo = load_unfilled_memo()
    qty_before = {h.ticker: h.qty for h in triggered}
    targets = [
        OrderTarget(side="SELL", ticker=h.ticker, name=h.name, qty=h.qty)
        for h in triggered
    ]

    with tee_capture() as buf:
        print(SEP_LINE)
        print(f"🚨 손절 트리거 — {len(triggered)}종목 시장가 매도")
        print(SEP_LINE)
        for h in triggered:
            print(
                f"  • {h.ticker} {h.name}: 수익률 {h.profit_rate:+.2f}%"
                f" ≤ {STOP_LOSS_PCT:+.2f}% ({h.qty:,}주, 평가 {h.eval_amount:,}원)"
            )
        print()
        orders = place_orders(api, targets)
        print()
        print(f"⏳ 체결 대기 {SETTLEMENT_WAIT}초 후 잔고 재조회")
        time.sleep(SETTLEMENT_WAIT)
        qty_after = _query_qty_after(api)
        fills = build_fill_results(orders, qty_before, qty_after)
        fully, partially = apply_fill_results_to_memo(memo, fills, orders)
        print()
        _log_fill_report(fills, orders, fully, partially)

    # 같은날 재주문 방지 (체결 결과와 무관 — 미체결은 내일 아침봇이 받음)
    for h in triggered:
        attempted_today.add(h.ticker)
    save_unfilled_memo(memo)

    title = f"[{mode}] [{STRATEGY_NAME}] 🚨 장중 손절 발생"
    header = (
        f"🚨 [{STRATEGY_NAME}] 장중 손절 알림\n"
        f"🕐 {_now_kst_str()}\n\n"
    )
    send_message(header + buf.getvalue(), title=title)
    return len(fully), len(partially)


def _log_fill_report(
    fills: list[FillResult],
    orders: list[OrderResult],
    fully: list[FillResult],
    partially: list[FillResult],
) -> None:
    print(SEP_LINE)
    print("🎯 손절 체결 검증 결과")
    print(SEP_LINE)
    for idx, (f, o) in enumerate(zip(fills, orders), 1):
        log_fill_card(f, o, idx)
    print()
    print(SEP_LINE)
    print(f"✅ 손절 완료: {len(fully)}/{len(fills)} 종목")
    if partially:
        print()
        print("📌 미체결 → SELL 장부 이월 (다음 영업일 아침 0순위 재청산):")
        for f in partially:
            print(f"  • {f.ticker} {f.name} 잔여 {f.unfilled_qty:,}주")


# ── 메인 루프 ─────────────────────────────────────────────────
def main() -> None:
    api, cfg = build_api()
    mode = "모의투자" if cfg.is_paper else "실전투자"

    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(
        f"파라미터: stop_loss={STOP_LOSS_PCT}%,"
        f" poll={INTRADAY_POLL_INTERVAL}s,"
        f" end={INTRADAY_END_HMS} KST,"
        f" settlement_wait={SETTLEMENT_WAIT}s"
    )
    print(f"미체결 메모 파일: {UNFILLED_MEMO_FILE}")
    print(SEP_LINE)
    print(f"🚀 장중 손절봇 기동 — {_now_kst_str()}")
    print(SEP_LINE)

    send_message(
        f"🚀 [{STRATEGY_NAME}] 장중 손절봇 기동\n"
        f"🕐 {_now_kst_str()}\n\n"
        f"• 손절 임계값: {STOP_LOSS_PCT:+.2f}%\n"
        f"• 폴링 주기:   {INTRADAY_POLL_INTERVAL}초\n"
        f"• 종료 예정:   {INTRADAY_END_HMS} KST\n"
        f"• 모드:        {mode}\n"
        f"• 계좌:        {cfg.account_full}",
        title=f"[{mode}] [{STRATEGY_NAME}] 장중 손절봇 기동",
    )

    attempted_today: set[str] = set()
    total_fully = 0
    total_partial = 0
    cycle = 0

    try:
        while should_continue_intraday():
            cycle += 1
            try:
                holdings = fetch_holdings(api)
            except Exception as exc:
                print(
                    f"[cycle {cycle}] ⚠️ 잔고 조회 실패: {exc}"
                    f" — {INTRADAY_POLL_INTERVAL}초 후 재시도"
                )
                time.sleep(INTRADAY_POLL_INTERVAL)
                continue

            triggered = pick_stop_loss_triggers(holdings, attempted_today)
            if not triggered:
                # 하트비트 로그 (텔레그램은 발송 안 함)
                worst = (
                    min((h.profit_rate for h in holdings), default=0.0)
                    if holdings else 0.0
                )
                print(
                    f"[cycle {cycle}] {_now_kst_str()} —"
                    f" 보유 {len(holdings)}종목,"
                    f" 최저 수익률 {worst:+.2f}%,"
                    f" 손절 처리 누계 {total_fully}건"
                    f" (오늘 처리큐 {len(attempted_today)})"
                )
                time.sleep(INTRADAY_POLL_INTERVAL)
                continue

            try:
                fully, partial = handle_stop_loss(
                    api, triggered, attempted_today, mode
                )
                total_fully += fully
                total_partial += partial
            except Exception as exc:
                # 손절 처리 중 예외 — 트레이스만 남기고 다음 사이클로
                print(f"[cycle {cycle}] ⚠️ 손절 처리 예외: {exc}")
                traceback.print_exc()

            time.sleep(INTRADAY_POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n⏹️ 사용자 인터럽트 — 정리 후 종료")

    # ── 종료 리포트 ──────────────────────────────────────────
    print()
    print(SEP_LINE)
    print(f"🏁 장중 손절봇 종료 — {_now_kst_str()}")
    print(SEP_LINE)
    print(f"• 총 사이클:       {cycle}")
    print(f"• 손절 완전체결:   {total_fully}건")
    print(f"• 손절 부분/미체결: {total_partial}건 (다음 영업일 아침 0순위)")
    print(f"• 오늘 처리큐:     {len(attempted_today)}종목")

    send_message(
        f"🏁 [{STRATEGY_NAME}] 장중 손절봇 종료\n"
        f"🕐 {_now_kst_str()}\n\n"
        f"• 총 사이클:       {cycle}\n"
        f"• 손절 완전체결:   {total_fully}건\n"
        f"• 손절 부분/미체결: {total_partial}건\n"
        f"• 오늘 처리큐:     {len(attempted_today)}종목\n\n"
        f"※ EOD봇(15:15)이 손절 안전망 + 20MA 청산을 이어서 처리합니다.",
        title=f"[{mode}] [{STRATEGY_NAME}] 장중 손절봇 종료",
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

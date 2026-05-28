"""morning_bot.py — 08:55 KST 1회 실행. 미체결 SELL/BUY 0순위 + 스마트 대기.

흐름(매수 시그널 누락 절대 금지 정신):
    1. 잔고 조회 → "장 시작 전 포트폴리오" 텔레그램 전송
    2. 장부(unfilled_orders.json) 로드
       - SELL: 어제 못 판 종목 (eod_exit_bot 이 남긴 잔여 수량)
       - BUY:  어제 못 산 종목 (peakeasy_r_bot 이 남긴 잔여 수량)
    3. 동시호가 진입 주문:
       a) SELL 시장가 매도 → 예수금/슬롯 확보
       b) BUY 시장가 매수
       각 주문 사이 1초 간격 + 실패 시 지수 백오프(2/4/8s) 로 최대 3회 재시도.
       3회 모두 실패한 주문은 OrderResult.accepted=False 로 수량 보존 → 장부 롤오버.
    4. 스마트 대기: 현재 KST 시각이 "09:01:00" 이 될 때까지 단일 sleep
       (정규장 개시 + 동시호가 체결 결과가 잔고에 반영되는 시점)
    5. 잔고 재조회 → BUY/SELL 잔고 델타로 체결 검증
       - 완전 체결: 장부에서 제거
       - 미체결/접수실패: 잔여 수량으로 장부 이월
    6. 체결 검증 리포트 텔레그램 전송 + 장부 저장 + sys.exit(0)

본 스크립트는 무한 루프/스케줄러 없이 싱글샷. crontab 으로 08:55 KST 1회 실행.
"""

from __future__ import annotations

import sys
import time

from shared_utils import (
    RATE_LIMIT_SLEEP,
    SEP_LINE,
    SETTLEMENT_WAIT,
    STRATEGY_NAME,
    UNFILLED_MEMO_FILE,
    FillResult,
    OrderResult,
    OrderTarget,
    apply_fill_results_to_memo,
    build_api,
    build_fill_results,
    fetch_account_summary,
    fetch_holdings,
    load_unfilled_memo,
    log_fill_card,
    memo_targets,
    place_orders,
    print_dashboard,
    print_post_liquidation_panel,
    save_unfilled_memo,
    snapshot_holdings_dict,
    wait_until_kst,
    _now_kst_str,
    _query_qty_after,
)
from telegram_notifier import send_message, tee_capture

# 정규장 개시(09:00) + 1분 마진 — 동시호가 체결 결과가 잔고에 반영되는 시점
SMART_WAIT_TARGET = "09:01:00"


def _build_targets(
    memo_buckets: dict, holdings_map: dict[str, dict]
) -> list[OrderTarget]:
    """장부에서 SELL → BUY 순으로 주문 타겟 시퀀스를 만든다.

    SELL 의 qty 는 (memo qty, 실보유수량) 중 작은 값으로 캡 (잔고 초과 매도 방지).
    실보유 0 이면 SELL 타겟에서 제외하고 장부에서도 즉시 정리한다.
    """
    targets: list[OrderTarget] = []

    # SELL 먼저
    for ticker, name, memo_qty in memo_targets(memo_buckets, "SELL"):
        held = holdings_map.get(ticker, {}).get("qty", 0)
        if held <= 0:
            print(f"  ✓ SELL 장부 {ticker} {name} — 실보유 0 (이미 청산됨) → 장부 정리")
            memo_buckets["SELL"].pop(ticker, None)
            continue
        qty = min(int(memo_qty) if memo_qty > 0 else held, held)
        targets.append(OrderTarget(side="SELL", ticker=ticker, name=name, qty=qty))

    # BUY
    for ticker, name, memo_qty in memo_targets(memo_buckets, "BUY"):
        if memo_qty <= 0:
            print(f"  ! BUY 장부 {ticker} {name} — 수량 0/누락 → 장부 정리(매수 불가)")
            memo_buckets["BUY"].pop(ticker, None)
            continue
        targets.append(OrderTarget(side="BUY", ticker=ticker, name=name, qty=int(memo_qty)))

    return targets


def _log_priority_summary(targets: list[OrderTarget]) -> None:
    sells = [t for t in targets if t.side == "SELL"]
    buys = [t for t in targets if t.side == "BUY"]
    print()
    print(SEP_LINE)
    print("🎯 동시호가 0순위 주문 큐")
    print(SEP_LINE)
    print(f"• 매도(SELL): {len(sells)}건")
    for t in sells:
        print(f"    - {t.ticker} {t.name} × {t.qty:,}주")
    print(f"• 매수(BUY):  {len(buys)}건")
    for t in buys:
        print(f"    - {t.ticker} {t.name} × {t.qty:,}주")


def _log_fill_report(
    fills: list[FillResult],
    orders: list[OrderResult],
    fully: list[FillResult],
    partially: list[FillResult],
) -> None:
    print(SEP_LINE)
    print("🎯 동시호가 체결 검증 결과")
    print(SEP_LINE)
    if not fills:
        print("(주문 없음)")
        return
    for idx, (f, o) in enumerate(zip(fills, orders), 1):
        log_fill_card(f, o, idx)
    print()
    print(SEP_LINE)
    print(f"✅ 체결 완료: {len(fully)}/{len(fills)} 건")
    if partially:
        print()
        print("📌 미체결 → 장부 이월 (다음 영업일 morning_bot 재시도):")
        for f in partially:
            verb = "매수" if f.side == "BUY" else "매도"
            print(
                f"  • [{f.side}] {f.ticker} {f.name} 미체결 {f.unfilled_qty:,}주"
                f" ({verb} 잔여)"
            )


def main() -> None:
    api, cfg = build_api()
    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌 {cfg.account_full}")
    print(f"미체결 메모 파일: {UNFILLED_MEMO_FILE}")

    memo = load_unfilled_memo()

    # ── [1/5] 잔고 조회 & 장 시작 전 대시보드 ────────────────────────
    print()
    print("[1/5] 잔고 조회 & 장 시작 전 대시보드 출력")
    holdings = fetch_holdings(api)
    with tee_capture() as buf:
        print_dashboard(api, holdings, is_morning=True)
    send_message(
        buf.getvalue(),
        title=f"[{mode}] [{STRATEGY_NAME}] 장 시작 전 포트폴리오",
    )

    # ── [2/5] 장부 → 주문 타겟 구축 ──────────────────────────────
    print()
    print("[2/5] 미체결 장부 → 0순위 주문 타겟 구축")
    holdings_map = snapshot_holdings_dict(api)
    targets = _build_targets(memo, holdings_map)
    if not targets:
        print("  장부 비어있음 또는 처리할 항목 없음 — 주문 없이 종료")
        save_unfilled_memo(memo)
        send_message(
            f"🌅 [{STRATEGY_NAME}] 아침 0순위 처리: 장부 비어 있음. 신규 주문 없음.",
            title=f"[{mode}] [{STRATEGY_NAME}] 아침 0순위 결과",
        )
        sys.exit(0)

    _log_priority_summary(targets)

    # 주문 직전 잔고 스냅샷(체결 검증 baseline)
    qty_before = {t: int(d["qty"]) for t, d in holdings_map.items()}

    # ── [3/5] 시장가 주문 (SELL → BUY 순) ──────────────────────────
    print()
    print(f"[3/5] 시장가 주문 ({len(targets)}건, SELL → BUY 순)")
    orders = place_orders(api, targets)

    # ── [4/5] 스마트 대기: 09:01 KST ─────────────────────────────────
    print()
    print(f"[4/5] 스마트 대기 — 정규장 개시 + 동시호가 체결 반영({SMART_WAIT_TARGET} KST)")
    wait_until_kst(SMART_WAIT_TARGET)

    # ── [5/5] 잔고 재조회 → 체결 검증 → 장부 갱신 ──────────────────
    print()
    print("[5/5] 잔고 재조회 → 체결 검증 → 장부 이월")
    # KIS 잔고 시스템에 체결이 반영될 시간 확보 — 너무 빨리 조회하면 부분체결로 오인
    print(f"  ⏳ 체결 반영 대기 — {SETTLEMENT_WAIT}초")
    time.sleep(SETTLEMENT_WAIT)
    qty_after = _query_qty_after(api)
    fills = build_fill_results(orders, qty_before, qty_after)
    fully, partially = apply_fill_results_to_memo(memo, fills, orders)

    # 텔레그램 리포트 합본
    report_chunks: list[str] = []
    with tee_capture() as buf:
        print(f"🌅 [{STRATEGY_NAME}] 아침 0순위 동시호가 결과 리포트")
        print(f"🕐 {_now_kst_str()}")
        print()
    report_chunks.append(buf.getvalue())

    with tee_capture() as buf:
        _log_fill_report(fills, orders, fully, partially)
    report_chunks.append(buf.getvalue())

    with tee_capture() as buf:
        print()
        print_post_liquidation_panel(fetch_account_summary(api))
    report_chunks.append(buf.getvalue())

    send_message(
        "".join(report_chunks),
        title=f"[{mode}] [{STRATEGY_NAME}] 아침 0순위 동시호가 결과",
    )

    save_unfilled_memo(memo)
    print(
        f"\n장부 저장 완료 — BUY 잔존 {len(memo.get('BUY', {}))}건,"
        f" SELL 잔존 {len(memo.get('SELL', {}))}건"
    )
    print("🌅 아침 0순위 처리 완료. 정상 종료합니다.")
    sys.exit(0)


if __name__ == "__main__":
    main()

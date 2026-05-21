"""eod_exit_bot.py — 장 마감 직전(15:15 부근) 1회 실행되는 종가 기준 청산 봇.

흐름:
    0. (전일 누적) unfilled_orders.json 에 기록된 미체결 종목을 0순위로
       무조건 시장가 청산 (조건 심사 없음). 청산 확인된 종목은 메모에서 제거.
    1. 보유 잔고 조회 → 대시보드(DataFrame) 출력
       (종목명 / 매입단가 / 현재가 / 보유수량 / 수익률)
    2. 보유 종목별 일봉 데이터 조회 → 20일 이동평균(20MA) 산출
    3. 청산 조건 판단 (실행 시점의 현재가 = 종가 근사치)
        - A: 수익률 ≤ -8.0% (손절)
        - B: 현재가 < 20MA (추세이탈)
       A 또는 B 중 하나라도 만족하면 청산 대상
       (단, 0순위에서 이미 시도한 종목은 일반 심사에서 제외)
    4. 청산 대상 전 수량을 시장가로 매도
       (API rate limit 고려, 주문 호출 사이 time.sleep(0.5))
    5. 매도 직후 time.sleep(3) 대기 → 잔고 재조회 → 0주 여부로 체결 검증
       잔여 수량(qty_after > 0) 종목은 unfilled_orders.json 에 누적 저장
       → 다음 영업일 실행 시 자동으로 0순위 청산 대상이 됨

본 스크립트는 무한 루프/스케줄러를 포함하지 않는 싱글샷 실행이다.
스케줄링은 cron/systemd-timer 등 OS 레벨 도구로 15:15 에 1회 실행을 권장.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pykis import Api, DomainInfo

from config import load_config
from telegram_notifier import send_message, tee_capture

# ── 튜닝 파라미터 ────────────────────────────────────────────────
RATE_LIMIT_SLEEP = 0.5      # API 호출 사이 최소 간격 (초)
SETTLEMENT_WAIT = 3          # 매도 주문 후 체결 대기 (초)
STOP_LOSS_PCT = -8.0         # 손절 임계 (수익률 %, 이 값 이하면 청산)
MA_WINDOW = 20               # 이동평균 기간 (영업일)
MAX_POSITIONS = 5            # 동시 보유 가능 종목 수 (peakeasy_r_bot.py 와 일치)
RISK_PER_TRADE_PCT = 1.6     # 1R 비중 (%, 전략 명세서 표기용)
STRATEGY_NAME = "52주 신고가 전략"
SEP_LINE = "━" * 32

# 미체결 메모: 시장가 주문 접수에도 잔여 수량이 남은 종목을 누적 저장.
# 다음 영업일 실행 시 가장 먼저 0순위 청산 대상으로 처리된다.
UNFILLED_MEMO_FILE = "unfilled_orders.json"
_MEMO_PATH = Path(__file__).resolve().parent / UNFILLED_MEMO_FILE


# ── 데이터 클래스 ────────────────────────────────────────────────
@dataclass
class Holding:
    """현재 보유 중인 단일 종목의 스냅샷."""
    ticker: str
    name: str
    qty: int
    avg_price: float
    current_price: int
    profit_rate: float  # %

    @property
    def purchase_amount(self) -> int:
        """매입금액 = 매입단가 × 수량."""
        return int(round(self.avg_price * self.qty))

    @property
    def eval_amount(self) -> int:
        """평가금액 = 현재가 × 수량."""
        return int(self.current_price * self.qty)

    @property
    def profit_amount(self) -> int:
        """종목 수익금 = 평가금액 - 매입금액."""
        return self.eval_amount - self.purchase_amount


@dataclass
class AccountSummary:
    """KIS 잔고 요약 outputs[1] 에서 추출한 계좌 전체 금액 스냅샷.

    - total_eval:     총 자산(평가액)   ← tot_evlu_amt
    - total_purchase: 총 매입금액       ← pchs_amt_smtl_amt
    - total_pl:       총 평가손익       ← evlu_pfls_smtl_amt
    return_rate 는 (총 평가손익 / 총 매입금액) × 100 으로 역산 (분모 0 → None).
    """
    total_eval: int
    total_purchase: int
    total_pl: int

    @property
    def return_rate(self) -> float | None:
        if self.total_purchase <= 0:
            return None
        return self.total_pl / self.total_purchase * 100.0


@dataclass
class ExitDecision:
    """단일 종목에 대한 청산 판단 결과."""
    holding: Holding
    ma20: float | None
    reasons: list[str] = field(default_factory=list)

    @property
    def should_exit(self) -> bool:
        return bool(self.reasons)


@dataclass
class SellResult:
    """매도 주문 접수 결과."""
    ticker: str
    name: str
    qty: int
    accepted: bool = False
    order_no: str = ""
    order_time: str = ""
    error: str = ""


@dataclass
class FillResult:
    """체결 검증 결과 (잔여 수량 기준)."""
    ticker: str
    name: str
    qty_before: int
    qty_after: int

    @property
    def fully_liquidated(self) -> bool:
        return self.qty_after == 0


# ── API 빌더 ─────────────────────────────────────────────────────
def build_api() -> tuple[Api, object]:
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


# ── 미체결 메모 I/O ──────────────────────────────────────────────
def load_unfilled_memo() -> dict[str, dict]:
    """unfilled_orders.json 로드. 파일이 없거나 형식이 이상하면 빈 dict 반환."""
    if not _MEMO_PATH.exists():
        return {}
    try:
        with open(_MEMO_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ! {UNFILLED_MEMO_FILE} 읽기 실패: {exc} — 빈 메모로 진행")
        return {}
    if not isinstance(data, dict):
        print(f"  ! {UNFILLED_MEMO_FILE} 형식 이상 (dict 아님) — 빈 메모로 진행")
        return {}
    return data


def save_unfilled_memo(memo: dict[str, dict]) -> None:
    """unfilled_orders.json 에 현재 메모 상태를 덮어쓰기 저장."""
    with open(_MEMO_PATH, "w", encoding="utf-8") as f:
        json.dump(memo, f, ensure_ascii=False, indent=2)


def mark_unfilled(memo: dict[str, dict], ticker: str, name: str) -> None:
    """미체결 종목 1건을 메모에 기록 (중복은 덮어쓰며 타임스탬프 갱신)."""
    memo[ticker] = {"name": name, "recorded_at": date.today().isoformat()}


# ── 잔고 조회 ────────────────────────────────────────────────────
def fetch_holdings(api: Api) -> list[Holding]:
    """잔고 DataFrame 을 Holding 리스트로 변환. 보유수량 0 인 행은 제외."""
    df = api.get_kr_stock_balance()
    if df is None or df.empty:
        return []
    holdings: list[Holding] = []
    # pykis 0.7.0 잔고 컬럼: 종목명, 보유수량, 매도가능수량, 매입단가, 수익율, 현재가, ...
    for ticker, row in df.iterrows():
        qty = int(row["보유수량"])
        if qty <= 0:
            continue
        holdings.append(
            Holding(
                ticker=str(ticker),
                name=str(row["종목명"]),
                qty=qty,
                avg_price=float(row["매입단가"]),
                current_price=int(row["현재가"]),
                profit_rate=float(row["수익율"]),
            )
        )
    return holdings


def _query_qty_after(api: Api) -> dict[str, int]:
    """잔고에서 {종목코드: 보유수량} 매핑만 빠르게 추출 (체결 검증용)."""
    df = api.get_kr_stock_balance()
    qty_map: dict[str, int] = {}
    if df is None or df.empty:
        return qty_map
    for tk, row in df.iterrows():
        qty_map[str(tk)] = int(row["보유수량"])
    return qty_map


def _to_int(value: object) -> int:
    """문자열/숫자/None 을 안전하게 int 로 변환 (실패 시 0)."""
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def fetch_account_summary(api: Api) -> AccountSummary | None:
    """KIS 잔고 outputs[1] 에서 총 평가액·총 매입·총 손익을 추출.

    실패 시 None 반환. 호출자가 None 을 받으면 '조회 실패' 로 표기한다.
    """
    try:
        res = api._get_kr_total_balance()
        if not res.outputs or len(res.outputs) < 2 or not res.outputs[1]:
            return None
        summary = res.outputs[1][0]
        return AccountSummary(
            total_eval=_to_int(summary.get("tot_evlu_amt")),
            total_purchase=_to_int(summary.get("pchs_amt_smtl_amt")),
            total_pl=_to_int(summary.get("evlu_pfls_smtl_amt")),
        )
    except Exception:
        return None


def _now_kst_str() -> str:
    """리포트 헤더에 박을 현재 일시 문자열 (로컬 타임존 기준)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _profit_emoji(value: float) -> str:
    """양수 🔴 / 음수 🔵 / 0 ▫️  — 기존 코드 컨벤션 유지."""
    if value > 0:
        return "🔴"
    if value < 0:
        return "🔵"
    return "▫️"


def _format_return_rate(rate: float | None) -> str:
    """수익률 표시 — None 이면 'N/A', 아니면 +/- 부호 포함 소수 둘째 자리."""
    if rate is None:
        return "N/A"
    return f"{rate:+.2f}%"


def print_strategy_panel() -> None:
    """전략 요약 — 명세서 형태로 고정 출력."""
    print(SEP_LINE)
    print("📝 전략 요약")
    print(SEP_LINE)
    print(f"• 전략명: {STRATEGY_NAME}")
    print(f"• 최대 {MAX_POSITIONS}종목 제한")
    print(f"• 1R 비중: {RISK_PER_TRADE_PCT:.1f}%")
    print(f"• 고정 손절: {STOP_LOSS_PCT:+.1f}%")
    print(f"• {MA_WINDOW}MA 청산")


def print_account_summary_panel(
    summary: AccountSummary | None,
    *,
    title: str = "💰 계좌 요약",
    held: int | None = None,
) -> None:
    """계좌 요약 패널 — 총 자산/매입/손익/전체 수익률 4종 + (옵션) 슬롯 표기."""
    print(SEP_LINE)
    print(title)
    print(SEP_LINE)
    if summary is None:
        print("• 총 자산(평가액):   조회 실패")
        print("• 총 매입 금액:      조회 실패")
        print("• 총 평가 손익:      조회 실패")
        print("• 계좌 전체 수익률:  조회 실패")
    else:
        pl_emoji = _profit_emoji(summary.total_pl)
        rate_emoji = (
            _profit_emoji(summary.return_rate)
            if summary.return_rate is not None
            else "▫️"
        )
        print(f"• 총 자산(평가액):   {summary.total_eval:>15,} 원")
        print(f"• 총 매입 금액:      {summary.total_purchase:>15,} 원")
        print(f"• 총 평가 손익:      {pl_emoji} {summary.total_pl:>+14,} 원")
        print(
            f"• 계좌 전체 수익률:  {rate_emoji} "
            f"{_format_return_rate(summary.return_rate)}"
        )
    if held is not None:
        free = max(MAX_POSITIONS - held, 0)
        print(f"• 보유 종목 수:      {held} / {MAX_POSITIONS} (여유 슬롯 {free})")


def print_dashboard(api: Api, holdings: list[Holding]) -> None:
    """장 시작 전 포트폴리오 대시보드 — 모바일 세로형 카드."""
    # 헤더
    print(f"📊 [{STRATEGY_NAME}] 장 시작 전 포트폴리오")
    print(f"🕐 {_now_kst_str()}")
    print()

    # 전략 요약 (고정 명세서)
    print_strategy_panel()

    # 계좌 요약
    summary = fetch_account_summary(api)
    print()
    print_account_summary_panel(summary, held=len(holdings))

    # 보유 종목 현황 (세로형 카드)
    print()
    print(SEP_LINE)
    print(f"📊 보유 종목 현황 ({len(holdings)}/{MAX_POSITIONS})")
    print(SEP_LINE)

    if not holdings:
        print("(보유 종목 없음)")
        return

    for idx, h in enumerate(holdings, 1):
        pr_emoji = _profit_emoji(h.profit_rate)
        amt_emoji = _profit_emoji(h.profit_amount)
        print()
        print(f"[{idx}] {h.ticker} {h.name}")
        print(f"  • 매입단가:    {h.avg_price:>12,.0f} 원")
        print(f"  • 현재가:      {h.current_price:>12,} 원")
        print(f"  • 보유수량:    {h.qty:>12,} 주")
        print(f"  • 매입금액:    {h.purchase_amount:>12,} 원")
        print(f"  • 평가금액:    {h.eval_amount:>12,} 원")
        print(f"  • 종목 수익금: {amt_emoji} {h.profit_amount:>+11,} 원")
        print(f"  • 수익률:      {pr_emoji} {h.profit_rate:+.2f}%")


# ── 시장가 매도 헬퍼 ────────────────────────────────────────────
def _market_sell(api: Api, ticker: str, name: str, qty: int) -> SellResult:
    """단일 종목 전 수량을 시장가로 매도하고 SellResult 반환."""
    result = SellResult(ticker=ticker, name=name, qty=qty)
    try:
        # price=0 → pykis 가 ORD_DVSN=01(시장가) 로 변환
        resp = api.sell_kr_stock(ticker, qty, 0)
        result.order_no = str(resp.get("ODNO", "")).strip()
        result.order_time = str(resp.get("ORD_TMD", "")).strip()
        if result.order_no:
            result.accepted = True
            print(
                f"      → 접수 OK 주문번호 {result.order_no}"
                f" (주문시각 {result.order_time}, {qty}주 시장가 매도)"
            )
        else:
            result.error = f"응답에 ODNO 없음: {resp}"
            print(f"      → 접수 실패: {result.error}")
    except Exception as exc:
        result.error = str(exc)
        print(f"      → 접수 실패: {exc}")
    return result


# ── 0순위 청산 (전일 미체결 메모 처리) ─────────────────────────────
def priority_liquidate(api: Api, memo: dict[str, dict]) -> set[str]:
    """memo 에 적힌 종목을 조건 심사 없이 무조건 시장가 청산.

    동작:
        1. 현재 잔고를 조회해 메모 종목의 실보유수량을 확인
        2. 이미 0주인 종목은 메모에서 제거
        3. 나머지는 전량 시장가 매도 (호출 사이 RATE_LIMIT_SLEEP)
        4. SETTLEMENT_WAIT 초 대기 후 잔고 재조회로 검증
        5. 잔량 0 → 메모 삭제 / 잔량 > 0 → 메모에 잔존 (타임스탬프 갱신)

    Args:
        memo: 호출자가 보유하는 미체결 메모. in-place 로 갱신된다.

    Returns:
        실제로 매도 주문을 시도한 종목코드 집합.
        호출자는 이 집합을 일반 청산 심사에서 제외해 중복 주문을 방지한다.
    """
    attempted: set[str] = set()

    # 현재 잔고에서 메모 종목의 보유수량 파악
    current = {h.ticker: h for h in fetch_holdings(api)}

    already_clear: list[str] = []
    targets: list[Holding] = []
    for ticker, entry in list(memo.items()):
        memo_name = entry.get("name", "") if isinstance(entry, dict) else ""
        held = current.get(ticker)
        if held is None or held.qty <= 0:
            # 이미 잔량 없음 → 메모 정리 후 스킵
            already_clear.append(f"{ticker} {memo_name or '(이름 미상)'}")
            del memo[ticker]
            continue
        targets.append(held)

    if already_clear:
        print(
            f"  잔량 없어 메모 정리: {len(already_clear)}건 — "
            f"{', '.join(already_clear)}"
        )

    if not targets:
        print("  실제 청산이 필요한 종목 없음")
        return attempted

    print(f"  0순위 청산 대상: {len(targets)}종목 (조건 심사 없이 무조건 시장가)")

    orders: list[SellResult] = []
    for i, h in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] {h.ticker} {h.name} — {h.qty}주 시장가 매도")
        time.sleep(RATE_LIMIT_SLEEP)
        orders.append(_market_sell(api, h.ticker, h.name, h.qty))
        attempted.add(h.ticker)

    print(f"  체결 대기 {SETTLEMENT_WAIT}초")
    time.sleep(SETTLEMENT_WAIT)

    after = _query_qty_after(api)

    print()
    print("  0순위 청산 검증:")
    header = (
        f"  {'종목코드':<8}  {'종목명':<12}  {'주문번호':<12}  "
        f"{'이전수량':>8}  {'잔여수량':>8}  {'결과':<8}"
    )
    print(header)
    print("  " + "-" * 70)
    cleared = 0
    for h, o in zip(targets, orders):
        qty_after = after.get(h.ticker, 0)
        if not o.accepted:
            status = "접수실패"
        elif qty_after == 0:
            status = "청산완료"
            cleared += 1
        else:
            status = "미체결"
        print(
            f"  {h.ticker:<8}  {h.name:<12}  {(o.order_no or '-'):<12}  "
            f"{h.qty:>8,}  {qty_after:>8,}  {status:<8}"
        )

        # 메모 갱신: 청산 완료 → 제거 / 잔량 남음 → 유지(타임스탬프 갱신)
        if qty_after == 0:
            memo.pop(h.ticker, None)
        else:
            mark_unfilled(memo, h.ticker, h.name)

    print("  " + "-" * 70)
    print(
        f"  청산 완료 {cleared}/{len(targets)},"
        f" 잔존 {len(targets) - cleared}건은 메모 유지 (다음 영업일 재시도)"
    )
    return attempted


# ── 20MA 산출 & 청산 판단 ───────────────────────────────────────
def fetch_ma20(api: Api, ticker: str) -> float | None:
    """일봉 데이터에서 최근 MA_WINDOW 영업일 종가 평균을 계산."""
    df = api.get_kr_ohlcv(ticker, "D")
    if df is None or df.empty:
        return None
    # KIS 응답은 최신 날짜가 앞쪽에 오므로 명시적으로 오름차순 정렬
    df = df.sort_index()
    closes = df["Close"].tail(MA_WINDOW)
    if len(closes) < MA_WINDOW:
        return None
    return float(closes.mean())


def decide_exits(api: Api, holdings: list[Holding]) -> list[ExitDecision]:
    """보유 종목 각각에 대해 손절/추세이탈 조건을 평가."""
    decisions: list[ExitDecision] = []
    for h in holdings:
        time.sleep(RATE_LIMIT_SLEEP)
        ma20 = fetch_ma20(api, h.ticker)
        decision = ExitDecision(holding=h, ma20=ma20)

        # 조건 A: 손절
        if h.profit_rate <= STOP_LOSS_PCT:
            decision.reasons.append(
                f"손절(수익률 {h.profit_rate:+.2f}% ≤ {STOP_LOSS_PCT:+.2f}%)"
            )

        # 조건 B: 추세이탈 (20MA 산출 불가 시 판단 보류)
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
    """청산 판단 결과를 모바일 세로형 카드로 출력하고, 청산 대상 리스트를 반환."""
    print(SEP_LINE)
    print(f"🎯 {MA_WINDOW}MA 기준 종가 청산 판단")
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


# ── 시장가 매도 실행 (일반 플로우) ──────────────────────────────────
def execute_sells(api: Api, exits: list[ExitDecision]) -> list[SellResult]:
    """청산 대상 전 수량을 시장가로 매도. 주문 사이 RATE_LIMIT_SLEEP 대기."""
    results: list[SellResult] = []
    for i, d in enumerate(exits, 1):
        h = d.holding
        print(
            f"  [{i}/{len(exits)}] 매도 시도: {h.ticker} {h.name}"
            f" — 사유: {' / '.join(d.reasons)}"
        )
        time.sleep(RATE_LIMIT_SLEEP)
        results.append(_market_sell(api, h.ticker, h.name, h.qty))
    return results


# ── 체결 검증 ───────────────────────────────────────────────────
def verify_sells(
    api: Api, exits: list[ExitDecision], orders: list[SellResult]
) -> list[FillResult]:
    """잔고 재조회 후 매도 종목의 잔여 수량을 확인."""
    after = _query_qty_after(api)
    return [
        FillResult(
            ticker=d.holding.ticker,
            name=d.holding.name,
            qty_before=d.holding.qty,
            qty_after=after.get(d.holding.ticker, 0),
        )
        for d, _o in zip(exits, orders)
    ]


def log_fill_summary(fills: list[FillResult], orders: list[SellResult]) -> None:
    """청산 체결 검증 결과를 모바일 세로형 카드 + 불릿 리스트로 출력."""
    print(SEP_LINE)
    print("🎯 청산 체결 검증 결과")
    print(SEP_LINE)

    for idx, (f, o) in enumerate(zip(fills, orders), 1):
        if not o.accepted:
            status_emoji = "⚠️"
            status_text = "접수실패"
        elif f.fully_liquidated:
            status_emoji = "🔵"
            status_text = "청산완료(손절/이탈)"
        else:
            status_emoji = "🛡️"
            status_text = "미체결(잔여)"

        print()
        print(f"[{idx}] {f.ticker} {f.name}")
        print(f"  • 주문번호: {o.order_no or '-'}")
        print(f"  • 이전수량: {f.qty_before:,} 주")
        print(f"  • 잔여수량: {f.qty_after:,} 주")
        print(f"  • 결과:    {status_emoji} {status_text}")

    print()
    print(SEP_LINE)
    cleared = sum(1 for f, o in zip(fills, orders) if o.accepted and f.fully_liquidated)
    print(f"✅ 청산 완료: {cleared}/{len(fills)} 종목")

    unfilled = [
        (f, o) for f, o in zip(fills, orders) if o.accepted and not f.fully_liquidated
    ]
    if unfilled:
        print()
        print("📌 접수됐으나 잔여 수량 (메모 → 다음 영업일 0순위 청산):")
        for f, o in unfilled:
            print(f"  • {f.ticker} {f.name} 잔여 {f.qty_after}주 (주문 {o.order_no})")

    failed = [(f, o) for f, o in zip(fills, orders) if not o.accepted]
    if failed:
        print()
        print("⚠️ 접수 실패:")
        for f, o in failed:
            print(f"  • {f.ticker} {f.name}: {o.error}")


def print_post_liquidation_panel(summary: AccountSummary | None) -> None:
    """청산 후 계좌 요약 — 리포트 맨 마지막에 박는 결산 패널."""
    print(SEP_LINE)
    print("💰 청산 후 계좌 요약")
    print(SEP_LINE)
    if summary is None:
        print("• 총 자산(평가액):   조회 실패")
        print("• 계좌 전체 수익률:  조회 실패")
        return
    rate_emoji = (
        _profit_emoji(summary.return_rate)
        if summary.return_rate is not None
        else "▫️"
    )
    print(f"• 총 자산(평가액):   {summary.total_eval:>15,} 원")
    print(
        f"• 계좌 전체 수익률:  {rate_emoji} "
        f"{_format_return_rate(summary.return_rate)}"
    )


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

    # ── [0/5] 전일 미체결 0순위 청산 ──────────────────────────────
    print()
    print("[0/5] 전일 미체결 0순위 청산 (메모 처리)")
    skip_in_normal: set[str] = set()
    if memo:
        print(f"  unfilled_orders.json 로드: {len(memo)}건")
        skip_in_normal = priority_liquidate(api, memo)
        save_unfilled_memo(memo)
        print(f"  → {UNFILLED_MEMO_FILE} 저장 완료 (잔존 {len(memo)}건)")
    else:
        print("  unfilled_orders.json 비어있음 — 0순위 청산 생략")

    # ── [1/5] 잔고 조회 & 대시보드 ─────────────────────────────────
    print()
    print("[1/5] 잔고 조회 & 대시보드 출력")
    holdings = fetch_holdings(api)
    with tee_capture() as buf:
        print_dashboard(api, holdings)
    send_message(
        buf.getvalue(),
        title=f"[{mode}] [{STRATEGY_NAME}] 장 시작 전 포트폴리오",
    )

    # EOD 리포트(청산 판단 + 체결 검증 + 청산 후 요약)는 한 메시지로 합쳐 전송한다.
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
            print_post_liquidation_panel(fetch_account_summary(api))
        eod_chunks.append(buf.getvalue())
        send_message("".join(eod_chunks), title=eod_title)
        print("\n보유 종목이 없어 종료합니다.")
        return

    # ── [2/5] 20MA & 청산 조건 평가 (0순위 시도 종목 제외) ────────────
    print()
    print("[2/5] 20일 이동평균 산출 & 청산 조건 판단")
    if skip_in_normal:
        for h in holdings:
            if h.ticker in skip_in_normal:
                print(
                    f"  ⤳ {h.ticker} {h.name}: 0순위 청산을 이미 시도 — 일반 심사 제외"
                )
    eligible = [h for h in holdings if h.ticker not in skip_in_normal]
    if not eligible:
        with tee_capture() as buf:
            print(SEP_LINE)
            print("ℹ️ 일반 심사 대상 없음 (전부 0순위에서 처리)")
            print(SEP_LINE)
            print()
            print_post_liquidation_panel(fetch_account_summary(api))
        eod_chunks.append(buf.getvalue())
        send_message("".join(eod_chunks), title=eod_title)
        print("\n일반 심사 대상 없음 (모두 0순위에서 처리). 종료합니다.")
        return
    decisions = decide_exits(api, eligible)
    with tee_capture() as buf:
        exits = log_decisions(decisions)
    eod_chunks.append(buf.getvalue())

    if not exits:
        with tee_capture() as buf:
            print()
            print_post_liquidation_panel(fetch_account_summary(api))
        eod_chunks.append(buf.getvalue())
        send_message("".join(eod_chunks), title=eod_title)
        print("\n청산 대상 없음. 종료합니다.")
        return

    # ── [3/5] 시장가 매도 ────────────────────────────────────────
    print()
    print(f"[3/5] 시장가 매도 주문 ({len(exits)}종목)")
    orders = execute_sells(api, exits)

    # ── [4/5] 체결 검증 ──────────────────────────────────────────
    print()
    print(f"[4/5] 체결 대기 {SETTLEMENT_WAIT}초 후 잔고 재조회")
    time.sleep(SETTLEMENT_WAIT)
    fills = verify_sells(api, exits, orders)
    with tee_capture() as buf:
        print()
        log_fill_summary(fills, orders)
        print()
        print_post_liquidation_panel(fetch_account_summary(api))
    eod_chunks.append(buf.getvalue())
    send_message("".join(eod_chunks), title=eod_title)

    # ── [5/5] 미체결 메모 업데이트 ────────────────────────────────
    print()
    print("[5/5] 미체결 메모 업데이트")
    new_unfilled: list[str] = []
    for f, o in zip(fills, orders):
        if o.accepted and not f.fully_liquidated:
            mark_unfilled(memo, f.ticker, f.name)
            new_unfilled.append(f"{f.ticker} {f.name}")
    if new_unfilled:
        print(
            f"  신규 미체결 {len(new_unfilled)}건 메모 추가: "
            f"{', '.join(new_unfilled)}"
        )
    else:
        print("  신규 미체결 없음")
    save_unfilled_memo(memo)
    print(f"  → {UNFILLED_MEMO_FILE} 저장 완료 (총 {len(memo)}건)")


if __name__ == "__main__":
    main()

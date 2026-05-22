"""shared_utils.py — 4개 봇(morning / eod / peakeasy_r) 공용 유틸 (장부 2.0).

핵심 변경(2.0):
    * unfilled_orders.json 스키마 개편
        {"BUY": {ticker: {"name", "qty", "recorded_at"}},
         "SELL": {ticker: {"name", "qty", "recorded_at"}}}
    * 시장가 매수/매도 헬퍼 양방향 제공: _market_buy / _market_sell
    * "스마트 대기" 헬퍼: wait_until_kst("09:01:00")
    * 잔고 델타 기반 체결 검증: build_fill_results
    * 모바일 세로형 카드 출력 패널은 기존과 호환 유지

주문 + 검증 분리:
    morning_bot 은 [SELL 주문 → BUY 주문 → 스마트 대기 → 한 번에 검증] 흐름이라
    place_*_orders / build_fill_results / apply_fill_results_to_memo 를 작은
    빌딩 블록으로 노출한다. 일반 EOD 봇은 한 흐름에서 SELL → SETTLEMENT_WAIT → 검증.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

from pykis import Api, DomainInfo

from config import load_config

# ── 튜닝 파라미터 ────────────────────────────────────────────────
RATE_LIMIT_SLEEP = 0.5
SETTLEMENT_WAIT = 3
STOP_LOSS_PCT = -8.0
STOP_LOSS_RATIO = 0.08
MA_WINDOW = 20
MAX_POSITIONS = 5
RISK_PER_TRADE_PCT = 1.6
ONE_R_RATIO = RISK_PER_TRADE_PCT / 100.0
STRATEGY_NAME = "52주 신고가 전략"
SEP_LINE = "━" * 32

UNFILLED_MEMO_FILE = "unfilled_orders.json"
_MEMO_PATH = Path(__file__).resolve().parent / UNFILLED_MEMO_FILE

KST = timezone(timedelta(hours=9))

Side = Literal["BUY", "SELL"]
SIDES: tuple[Side, Side] = ("BUY", "SELL")


# ── 데이터 클래스 ────────────────────────────────────────────────
@dataclass
class Holding:
    ticker: str
    name: str
    qty: int
    avg_price: float
    current_price: int
    profit_rate: float  # %

    @property
    def purchase_amount(self) -> int:
        return int(round(self.avg_price * self.qty))

    @property
    def eval_amount(self) -> int:
        return int(self.current_price * self.qty)

    @property
    def profit_amount(self) -> int:
        return self.eval_amount - self.purchase_amount


@dataclass
class AccountSummary:
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
    holding: Holding
    ma20: float | None
    reasons: list[str] = field(default_factory=list)

    @property
    def should_exit(self) -> bool:
        return bool(self.reasons)


@dataclass
class OrderTarget:
    """주문 직전 단계의 (방향, 종목, 수량) 표현."""
    side: Side
    ticker: str
    name: str
    qty: int


@dataclass
class OrderResult:
    side: Side
    ticker: str
    name: str
    qty: int
    accepted: bool = False
    order_no: str = ""
    order_time: str = ""
    error: str = ""


@dataclass
class FillResult:
    """잔고 델타로 산출한 체결 검증 결과."""
    side: Side
    ticker: str
    name: str
    qty_ordered: int
    qty_before: int
    qty_after: int

    @property
    def filled_qty(self) -> int:
        if self.side == "BUY":
            return max(self.qty_after - self.qty_before, 0)
        return max(self.qty_before - self.qty_after, 0)

    @property
    def unfilled_qty(self) -> int:
        return max(self.qty_ordered - self.filled_qty, 0)

    @property
    def fully_filled(self) -> bool:
        return self.qty_ordered > 0 and self.unfilled_qty == 0


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


# ── 시간/KST 유틸 ────────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)


def _now_kst_str() -> str:
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


def wait_until_kst(target_hms: str) -> None:
    """현재 KST 시각이 target_hms 가 될 때까지 단일 sleep 으로 대기.

    target_hms 가 이미 지났으면 즉시 반환한다 (수동 실행 등).
    """
    parts = target_hms.split(":")
    if len(parts) == 2:
        parts.append("0")
    h, m, s = (int(x) for x in parts)
    now = now_kst()
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= now:
        print(
            f"  ⏩ 스마트 대기 스킵 — {target_hms} KST 이미 지남"
            f" (현재 {now.strftime('%H:%M:%S')} KST)"
        )
        return
    wait_sec = (target - now).total_seconds()
    print(
        f"  ⏳ {target_hms} KST 까지 스마트 대기 — {wait_sec:.0f}초"
        f" (현재 {now.strftime('%H:%M:%S')} KST)"
    )
    time.sleep(wait_sec)
    print(f"  ⏰ 대기 종료 — 현재 {now_kst().strftime('%H:%M:%S')} KST")


# ── 미체결 메모(장부) 2.0 I/O ────────────────────────────────────
def _empty_memo() -> dict[Side, dict[str, dict]]:
    return {"BUY": {}, "SELL": {}}


def _migrate_legacy_memo(data: dict) -> dict[Side, dict[str, dict]]:
    """구버전 평탄 {ticker:{...}} 포맷을 SELL 로 마이그레이션."""
    memo = _empty_memo()
    for k, v in data.items():
        if k in SIDES:
            continue  # 새 포맷은 _coerce 에서 처리
        if not isinstance(v, dict):
            continue
        memo["SELL"][str(k)] = {
            "name": str(v.get("name", "")),
            "qty": int(v.get("qty", 0) or 0),
            "recorded_at": str(v.get("recorded_at", date.today().isoformat())),
        }
    return memo


def _coerce_side_bucket(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        out[str(k)] = {
            "name": str(v.get("name", "")),
            "qty": int(v.get("qty", 0) or 0),
            "recorded_at": str(v.get("recorded_at", date.today().isoformat())),
        }
    return out


def load_unfilled_memo() -> dict[Side, dict[str, dict]]:
    """unfilled_orders.json 을 새 스키마 dict 로 로드. 구버전은 자동 마이그레이션."""
    if not _MEMO_PATH.exists():
        return _empty_memo()
    try:
        with open(_MEMO_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ! {UNFILLED_MEMO_FILE} 읽기 실패: {exc} — 빈 메모로 진행")
        return _empty_memo()
    if not isinstance(data, dict):
        print(f"  ! {UNFILLED_MEMO_FILE} 형식 이상 (dict 아님) — 빈 메모로 진행")
        return _empty_memo()

    # 새 포맷 판별: 최상위 키에 BUY 또는 SELL 이 있고, 값이 dict 면 새 포맷
    if any(k in SIDES and isinstance(data.get(k), dict) for k in SIDES):
        memo = _empty_memo()
        memo["BUY"] = _coerce_side_bucket(data.get("BUY"))
        memo["SELL"] = _coerce_side_bucket(data.get("SELL"))
        return memo

    # 구버전(평탄) → SELL 로 마이그레이션
    return _migrate_legacy_memo(data)


def save_unfilled_memo(memo: dict[Side, dict[str, dict]]) -> None:
    payload = {
        "BUY": memo.get("BUY", {}),
        "SELL": memo.get("SELL", {}),
    }
    with open(_MEMO_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def mark_unfilled(
    memo: dict[Side, dict[str, dict]],
    side: Side,
    ticker: str,
    name: str,
    qty: int,
) -> None:
    """미체결 1건을 메모에 기록 (덮어쓰기). qty 가 0 이하면 제거."""
    bucket = memo.setdefault(side, {})
    if qty <= 0:
        bucket.pop(ticker, None)
        return
    bucket[ticker] = {
        "name": name,
        "qty": int(qty),
        "recorded_at": date.today().isoformat(),
    }


def clear_unfilled(
    memo: dict[Side, dict[str, dict]],
    side: Side,
    ticker: str,
) -> None:
    memo.setdefault(side, {}).pop(ticker, None)


def memo_targets(
    memo: dict[Side, dict[str, dict]], side: Side
) -> list[tuple[str, str, int]]:
    """memo[side] 를 (ticker, name, qty) 리스트로 평탄화."""
    bucket = memo.get(side, {}) or {}
    out: list[tuple[str, str, int]] = []
    for ticker, entry in bucket.items():
        out.append((str(ticker), str(entry.get("name", "")), int(entry.get("qty", 0) or 0)))
    return out


# ── 잔고 조회 ────────────────────────────────────────────────────
def fetch_holdings(api: Api) -> list[Holding]:
    df = api.get_kr_stock_balance()
    if df is None or df.empty:
        return []
    holdings: list[Holding] = []
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
    df = api.get_kr_stock_balance()
    qty_map: dict[str, int] = {}
    if df is None or df.empty:
        return qty_map
    for tk, row in df.iterrows():
        qty_map[str(tk)] = int(row["보유수량"])
    return qty_map


def snapshot_holdings_dict(api: Api) -> dict[str, dict]:
    """{종목코드: {qty, avg_price, name}} 형식 스냅샷 (peakeasy 호환)."""
    df = api.get_kr_stock_balance()
    if df is None or df.empty:
        return {}
    out: dict[str, dict] = {}
    for ticker, row in df.iterrows():
        out[str(ticker)] = {
            "name": str(row.get("종목명", "")),
            "qty": int(row.get("보유수량", 0)),
            "avg_price": float(row.get("매입단가", 0.0)),
        }
    return out


def _to_int(value: object) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def fetch_account_summary(api: Api) -> AccountSummary | None:
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


def get_total_asset(api: Api) -> int:
    """총자산(예수금+주식평가) — peakeasy 사이징용. 실패 시 RuntimeError."""
    res = api._get_kr_total_balance()
    if not res.outputs or len(res.outputs) < 2 or not res.outputs[1]:
        raise RuntimeError("잔고 응답에서 요약(outputs[1])을 찾지 못함")
    return _to_int(res.outputs[1][0].get("tot_evlu_amt"))


# ── 출력 포맷 헬퍼 ───────────────────────────────────────────────
def _profit_emoji(value: float) -> str:
    if value > 0:
        return "🔴"
    if value < 0:
        return "🔵"
    return "▫️"


def _format_return_rate(rate: float | None) -> str:
    if rate is None:
        return "N/A"
    return f"{rate:+.2f}%"


def print_strategy_panel() -> None:
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
            if summary.return_rate is not None else "▫️"
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


def print_dashboard(api: Api, holdings: list[Holding], is_morning: bool = False) -> None:
    title_prefix = "장 시작 전" if is_morning else "장 마감 전"
    print(f"📊 [{STRATEGY_NAME}] {title_prefix} 포트폴리오")
    print(f"🕐 {_now_kst_str()}")
    print()
    print_strategy_panel()
    summary = fetch_account_summary(api)
    print()
    print_account_summary_panel(summary, held=len(holdings))
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


def print_post_liquidation_panel(summary: AccountSummary | None) -> None:
    print(SEP_LINE)
    print("💰 청산 후 계좌 요약")
    print(SEP_LINE)
    if summary is None:
        print("• 총 자산(평가액):   조회 실패")
        print("• 계좌 전체 수익률:  조회 실패")
        return
    rate_emoji = (
        _profit_emoji(summary.return_rate)
        if summary.return_rate is not None else "▫️"
    )
    print(f"• 총 자산(평가액):   {summary.total_eval:>15,} 원")
    print(
        f"• 계좌 전체 수익률:  {rate_emoji} "
        f"{_format_return_rate(summary.return_rate)}"
    )


# ── 시장가 매수/매도 헬퍼 ────────────────────────────────────────
def _market_sell(api: Api, ticker: str, name: str, qty: int) -> OrderResult:
    result = OrderResult(side="SELL", ticker=ticker, name=name, qty=qty)
    try:
        resp = api.sell_kr_stock(ticker, qty, 0)
        result.order_no = str(resp.get("ODNO", "")).strip()
        result.order_time = str(resp.get("ORD_TMD", "")).strip()
        if result.order_no:
            result.accepted = True
            print(
                f"      → 매도 접수 OK 주문번호 {result.order_no}"
                f" (주문시각 {result.order_time}, {qty}주 시장가)"
            )
        else:
            result.error = f"응답에 ODNO 없음: {resp}"
            print(f"      → 매도 접수 실패: {result.error}")
    except Exception as exc:
        result.error = str(exc)
        print(f"      → 매도 접수 실패: {exc}")
    return result


def _market_buy(api: Api, ticker: str, name: str, qty: int) -> OrderResult:
    result = OrderResult(side="BUY", ticker=ticker, name=name, qty=qty)
    try:
        resp = api.buy_kr_stock(ticker, qty, 0)
        result.order_no = str(resp.get("ODNO", "")).strip()
        result.order_time = str(resp.get("ORD_TMD", "")).strip()
        if result.order_no:
            result.accepted = True
            print(
                f"      → 매수 접수 OK 주문번호 {result.order_no}"
                f" (주문시각 {result.order_time}, {qty}주 시장가)"
            )
        else:
            result.error = f"응답에 ODNO 없음: {resp}"
            print(f"      → 매수 접수 실패: {result.error}")
    except Exception as exc:
        result.error = str(exc)
        print(f"      → 매수 접수 실패: {exc}")
    return result


def place_orders(api: Api, targets: Iterable[OrderTarget]) -> list[OrderResult]:
    """OrderTarget 시퀀스를 순서대로 시장가 주문하고 결과 리스트 반환."""
    results: list[OrderResult] = []
    target_list = list(targets)
    for i, t in enumerate(target_list, 1):
        verb = "매수" if t.side == "BUY" else "매도"
        print(f"  [{i}/{len(target_list)}] {verb} 시도: {t.ticker} {t.name} — {t.qty}주")
        time.sleep(RATE_LIMIT_SLEEP)
        if t.qty <= 0:
            results.append(OrderResult(
                side=t.side, ticker=t.ticker, name=t.name, qty=0,
                error=f"수량 0 — {verb} 스킵",
            ))
            print(f"      → 스킵: 수량 0")
            continue
        if t.side == "BUY":
            results.append(_market_buy(api, t.ticker, t.name, t.qty))
        else:
            results.append(_market_sell(api, t.ticker, t.name, t.qty))
    return results


# ── 체결 검증 ────────────────────────────────────────────────────
def build_fill_results(
    orders: list[OrderResult],
    qty_before: dict[str, int],
    qty_after: dict[str, int],
) -> list[FillResult]:
    fills: list[FillResult] = []
    for o in orders:
        fills.append(FillResult(
            side=o.side,
            ticker=o.ticker,
            name=o.name,
            qty_ordered=o.qty,
            qty_before=qty_before.get(o.ticker, 0),
            qty_after=qty_after.get(o.ticker, 0),
        ))
    return fills


def apply_fill_results_to_memo(
    memo: dict[Side, dict[str, dict]],
    fills: list[FillResult],
    orders: list[OrderResult],
) -> tuple[list[FillResult], list[FillResult]]:
    """체결 검증 결과를 memo 에 반영. (완료 리스트, 미체결 리스트) 반환.

    - 접수 성공 + 완전 체결 → memo 에서 제거
    - 접수 성공 + 부분/미체결 → memo 에 잔여 수량 갱신
    - 접수 실패 → memo 에 원래 수량 그대로 잔존(= 잔여 = 주문수량)
    """
    fully: list[FillResult] = []
    partially: list[FillResult] = []
    for f, o in zip(fills, orders):
        if not o.accepted:
            mark_unfilled(memo, f.side, f.ticker, f.name, f.qty_ordered)
            partially.append(f)
            continue
        if f.fully_filled:
            clear_unfilled(memo, f.side, f.ticker)
            fully.append(f)
        else:
            mark_unfilled(memo, f.side, f.ticker, f.name, f.unfilled_qty)
            partially.append(f)
    return fully, partially


def log_fill_card(f: FillResult, o: OrderResult, idx: int) -> None:
    """단일 체결 결과를 모바일 세로형 카드 1개로 출력."""
    verb = "매수" if f.side == "BUY" else "매도"
    if not o.accepted:
        status_emoji = "⚠️"
        status_text = "접수실패"
    elif f.fully_filled:
        status_emoji = "🔴" if f.side == "BUY" else "🔵"
        status_text = f"체결완료({verb})"
    elif f.filled_qty > 0:
        status_emoji = "🟡"
        status_text = f"부분체결({verb})"
    else:
        status_emoji = "🛡️"
        status_text = f"미체결({verb})"
    print()
    print(f"[{idx}] {f.ticker} {f.name}")
    print(f"  • 방향:    {verb}")
    print(f"  • 주문번호: {o.order_no or '-'}")
    print(f"  • 주문수량: {f.qty_ordered:,} 주")
    print(f"  • 이전수량: {f.qty_before:,} 주")
    print(f"  • 현재수량: {f.qty_after:,} 주")
    print(f"  • 체결수량: {f.filled_qty:,} 주")
    print(f"  • 미체결:   {f.unfilled_qty:,} 주")
    print(f"  • 결과:    {status_emoji} {status_text}")
    if not o.accepted and o.error:
        print(f"  • 에러:    {o.error}")

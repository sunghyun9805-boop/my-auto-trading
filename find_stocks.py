"""KOSPI 200 종목 중 현재가가 52주 최고가에 근접(>=95%)한 종목 탐색.

- 시세 데이터: pykis (한국투자증권 Open API) 의 inquire-price 엔드포인트
  → 현재가(stck_prpr) 와 52주 최고가(w52_hgpr) 를 한 번에 조회.
- KOSPI 200 종목 리스트: 네이버 금융 KPI200 페이지 스크레이핑.
  (pykrx 1.2.x 는 KRX 로그인 자격증명을 요구하므로 사용 불가)

매수는 하지 않고, 조건을 만족하는 종목만 표 형태로 출력한다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from pykis import Api, DomainInfo

from config import load_config

# 52주 최고가 대비 현재가 비율 임계값 (이 값 이상이면 "근접"으로 판정)
PROXIMITY_THRESHOLD = 0.95

# pykis 호출 간 간격 (KIS API rate limit 회피)
CALL_INTERVAL_SEC = 0.06

NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}
NAVER_URL = "https://finance.naver.com/sise/entryJongmok.naver"


@dataclass
class StockSnapshot:
    ticker: str
    name: str
    current: int
    high_52w: int

    @property
    def pct_of_high(self) -> float:
        return self.current / self.high_52w * 100.0


def fetch_kospi200_tickers() -> list[tuple[str, str]]:
    """네이버 금융에서 KOSPI 200 종목코드/종목명을 모두 가져온다."""
    pairs: dict[str, str] = {}  # ticker -> name (중복 제거)
    # KOSPI 200 은 한 페이지당 10종목, 총 20페이지 정도. 안전하게 22까지 시도.
    pattern = re.compile(
        r'/item/main\.naver\?code=(?P<code>\d{6})"[^>]*>(?P<name>[^<]+)</a>'
    )
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
        # 새로 추가된 게 없으면 마지막 페이지에 도달한 것으로 보고 중단
        if len(pairs) == before:
            break
    return list(pairs.items())


def build_api():
    cfg = load_config()
    return Api(
        key_info={"appkey": cfg.app_key, "appsecret": cfg.app_secret},
        domain_info=DomainInfo(kind="virtual" if cfg.is_paper else "real"),
        account_info={
            "account_code": cfg.account_no,
            "product_code": cfg.account_product_code,
        },
    ), cfg


def fetch_snapshot(api: Api, ticker: str, name: str) -> StockSnapshot | None:
    """단일 종목의 현재가 + 52주 최고가 조회. 실패 시 None."""
    try:
        info = api._get_kr_stock_current_price_info(ticker)
        current = int(info["stck_prpr"])
        high_52w = int(info["w52_hgpr"])
        if high_52w <= 0:
            return None
        return StockSnapshot(ticker=ticker, name=name, current=current, high_52w=high_52w)
    except Exception as exc:
        print(f"  ! {ticker} {name} 조회 실패: {exc}")
        return None


def main() -> None:
    print("[1/3] KOSPI 200 종목 목록 수집 중...")
    tickers = fetch_kospi200_tickers()
    print(f"      → {len(tickers)}개 종목 확보")

    print("[2/3] pykis 로 시세 조회 중...")
    api, cfg = build_api()
    print(f"      ({'모의투자' if cfg.is_paper else '실전투자'} 도메인)")

    snapshots: list[StockSnapshot] = []
    for i, (ticker, name) in enumerate(tickers, 1):
        snap = fetch_snapshot(api, ticker, name)
        if snap is not None:
            snapshots.append(snap)
        if i % 20 == 0:
            print(f"      ... {i}/{len(tickers)} 진행")
        time.sleep(CALL_INTERVAL_SEC)

    print(f"      → {len(snapshots)}개 종목 시세 수집 완료")

    print("[3/3] 52주 최고가 대비 95% 이상 종목 필터링")
    near_high = [s for s in snapshots if s.pct_of_high >= PROXIMITY_THRESHOLD * 100]
    near_high.sort(key=lambda s: s.pct_of_high, reverse=True)

    print()
    if not near_high:
        print("조건(현재가 >= 52주 최고가의 95%)에 해당하는 종목이 없습니다.")
        return

    print(f"=== 52주 최고가 근접 종목 ({len(near_high)}개) ===")
    header = f"{'종목코드':<8}  {'종목명':<14}  {'현재가':>10}  {'52주 최고':>10}  {'비율':>7}"
    print(header)
    print("-" * len(header))
    for s in near_high:
        print(
            f"{s.ticker:<8}  {s.name:<14}  "
            f"{s.current:>10,}  {s.high_52w:>10,}  "
            f"{s.pct_of_high:>6.2f}%"
        )


if __name__ == "__main__":
    main()

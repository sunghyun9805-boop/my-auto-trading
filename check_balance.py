"""계좌 잔고 조회.

config.py 의 설정을 사용해 한국투자증권 Open API 에 접속하여
계좌의 '총 평가금액' 과 '보유 현금(예수금)' 을 출력한다.
"""

from __future__ import annotations

from pykis import Api, DomainInfo

from config import load_config


def main() -> None:
    cfg = load_config()

    api = Api(
        key_info={
            "appkey": cfg.app_key,
            "appsecret": cfg.app_secret,
        },
        domain_info=DomainInfo(kind="virtual" if cfg.is_paper else "real"),
        account_info={
            "account_code": cfg.account_no,
            "product_code": cfg.account_product_code,
        },
    )

    # 잔고 조회 API 한 번 호출로 두 값 모두 추출 (output2 에 요약 정보가 들어있음)
    res = api._get_kr_total_balance()
    summary = res.outputs[1][0]

    total_eval = int(summary["tot_evlu_amt"])  # 총 평가금액
    deposit = int(summary["dnca_tot_amt"])      # 예수금 총액 = 보유 현금

    mode = "모의투자" if cfg.is_paper else "실전투자"
    print(f"[{mode}] 계좌번호: {cfg.account_full}")
    print(f"총 평가금액 : {total_eval:>15,} 원")
    print(f"보유 현금   : {deposit:>15,} 원")


if __name__ == "__main__":
    main()

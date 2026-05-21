"""한국투자증권 Open API 설정 로더.

`.env` 파일에서 인증 정보를 읽어와 안전하게 제공한다.
실수로 키가 비어 있는 채로 실행되는 일이 없도록 로드 시점에 검증한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트의 .env 파일을 로드
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


class ConfigError(RuntimeError):
    """필수 환경변수가 누락되었을 때 발생."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("your_"):
        raise ConfigError(
            f"환경변수 '{name}' 가 설정되지 않았습니다. "
            f".env 파일을 확인하세요. (참고: .env.example)"
        )
    return value


@dataclass(frozen=True)
class KISConfig:
    app_key: str
    app_secret: str
    account_no: str
    account_product_code: str
    env: str  # "real" 또는 "paper"
    base_url: str
    # 텔레그램 알림은 선택사항. 둘 중 하나라도 비어 있으면 알림은 비활성화된다.
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @property
    def is_paper(self) -> bool:
        return self.env == "paper"

    @property
    def account_full(self) -> str:
        """계좌번호 전체 (앞 8자리 + 뒤 2자리)."""
        return f"{self.account_no}-{self.account_product_code}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def load_config() -> KISConfig:
    env = os.getenv("KIS_ENV", "paper").strip().lower()
    if env not in {"real", "paper"}:
        raise ConfigError(f"KIS_ENV 는 'real' 또는 'paper' 여야 합니다. (현재: {env!r})")

    real_url = os.getenv("KIS_REAL_URL", "https://openapi.koreainvestment.com:9443")
    paper_url = os.getenv("KIS_PAPER_URL", "https://openapivts.koreainvestment.com:29443")
    base_url = real_url if env == "real" else paper_url

    return KISConfig(
        app_key=_require("KIS_APP_KEY"),
        app_secret=_require("KIS_APP_SECRET"),
        account_no=_require("KIS_ACCOUNT_NO"),
        account_product_code=os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01").strip(),
        env=env,
        base_url=base_url,
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    )


if __name__ == "__main__":
    # 설정이 제대로 로드되는지 빠르게 확인 (시크릿은 마스킹)
    cfg = load_config()
    print(f"환경: {cfg.env} ({'모의투자' if cfg.is_paper else '실전투자'})")
    print(f"베이스 URL: {cfg.base_url}")
    print(f"App Key: {cfg.app_key[:4]}{'*' * (len(cfg.app_key) - 4)}")
    print(f"계좌번호: {cfg.account_full}")

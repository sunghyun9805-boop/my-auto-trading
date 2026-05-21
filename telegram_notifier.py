"""telegram_notifier.py — 매매 봇용 텔레그램 동기 전송 헬퍼.

설계 원칙:
    * 자격 증명(TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)이 비어 있으면 조용히 no-op.
      → .env 에 값을 채워 넣기 전에 봇이 깨지는 것을 막는다.
    * 전송 실패는 stderr 에만 기록하고 예외를 호출자에게 전파하지 않는다.
      → 텔레그램 장애가 매매 로직을 멈춰서는 안 된다.
    * 표 형태의 출력은 HTML <pre> 블록으로 감싸 모바일에서도 정렬을 유지.
    * 텔레그램 단일 메시지 길이 한계(4096자)를 초과하면 줄 단위로 분할 전송.
"""

from __future__ import annotations

import html
import io
import sys
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

import requests

from config import load_config

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT = 10                # 초
# 4096 - <pre></pre> 태그 길이 - 헤더 여유분
MAX_BODY_PER_CHUNK = 3800


@lru_cache(maxsize=1)
def _credentials() -> tuple[str, str]:
    """(token, chat_id) 를 반환. 한 번 로드한 결과를 캐시."""
    cfg = load_config()
    return cfg.telegram_token, cfg.telegram_chat_id


def _post(token: str, chat_id: str, html_text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=SEND_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(
            f"Telegram HTTP {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def _chunk_lines(text: str, limit: int = MAX_BODY_PER_CHUNK) -> Iterator[str]:
    """본문을 줄 단위로 잘라 limit 글자 이하 청크 스트림으로 분할."""
    text = text.rstrip()
    if not text:
        return
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        # 단일 라인이 limit 보다 길면 강제로 잘라낸다 (드문 경우)
        while len(line) > limit:
            if current:
                yield "\n".join(current)
                current = []
                current_len = 0
            yield line[:limit]
            line = line[limit:]
        added = len(line) + (1 if current else 0)
        if current_len + added > limit:
            yield "\n".join(current)
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added
    if current:
        yield "\n".join(current)


def send_message(text: str, *, title: str | None = None) -> None:
    """텍스트를 텔레그램으로 전송. 자격 증명이 비어 있으면 no-op.

    Args:
        text: 모노스페이스(<pre>) 블록으로 감싸 전송할 본문.
        title: 옵션. 본문 앞에 굵게 표시할 제목.
    """
    if not text or not text.strip():
        return
    token, chat_id = _credentials()
    if not token or not chat_id:
        return  # 자격 증명 미설정 — 알림 비활성화

    chunks = list(_chunk_lines(text))
    total = len(chunks)
    try:
        for i, chunk in enumerate(chunks, 1):
            parts: list[str] = []
            if title:
                suffix = f" ({i}/{total})" if total > 1 else ""
                parts.append(f"<b>{html.escape(title + suffix)}</b>")
            parts.append(f"<pre>{html.escape(chunk)}</pre>")
            _post(token, chat_id, "\n".join(parts))
    except Exception as exc:
        print(f"[telegram_notifier] 전송 실패: {exc}", file=sys.stderr)


@contextmanager
def tee_capture() -> Iterator[io.StringIO]:
    """with 블록 안의 print 출력을 stdout 으로 흘리면서 동시에 버퍼에 누적.

    사용 예:
        with tee_capture() as buf:
            log_fill_summary(...)
        send_message(buf.getvalue(), title="...")
    """
    buf = io.StringIO()
    original = sys.stdout

    class _Tee:
        def write(self, s: str) -> int:
            original.write(s)
            buf.write(s)
            return len(s)

        def flush(self) -> None:
            original.flush()

    sys.stdout = _Tee()
    try:
        yield buf
    finally:
        sys.stdout = original

"""Public Telegram deep-link host (t.me may be blocked)."""
from __future__ import annotations

import os

_DEFAULT_HOST = "telegram.dog"
_LEGACY_HOSTS = ("t.me", "www.t.me")


def telegram_link_host() -> str:
    raw = (os.environ.get("TELEGRAM_LINK_HOST") or _DEFAULT_HOST).strip()
    raw = raw.removeprefix("https://").removeprefix("http://").rstrip("/")
    return raw or _DEFAULT_HOST


def telegram_url(path_and_query: str) -> str:
    return f"https://{telegram_link_host()}/{path_and_query.lstrip('/')}"


def normalize_telegram_url(url: str) -> str:
    """Rewrite legacy t.me links to the configured Telegram host."""
    text = (url or "").strip()
    if not text:
        return text
    for host in _LEGACY_HOSTS:
        for scheme in ("https://", "http://"):
            prefix = f"{scheme}{host}/"
            if text.lower().startswith(prefix):
                return telegram_url(text[len(prefix) :])
    return text

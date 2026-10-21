"""hotscope.tv catalog parser — /trending?page=N via Next.js RSC payload."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from peach_db import abs_url, db_conn, get_setting
from pipeline_config import MAX_BACKLOG_PAGES, MAX_EMPTY_PAGES, MAX_NEW, MAX_VIDEOS, REQUEST_DELAY_SEC
from queue_guard import parser_ingest_allowed

SITE = "https://hotscope.tv"
CDN = "https://cdn.hotscope.tv"
ORIGIN_DEFAULT = SITE
ID_PREFIX = "hs"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE}/trending",
}

VIDEO_OBJ_RE = re.compile(
    r'\{"_count":\{"likes":\d+,"comments":\d+\},"approved":true,"id":"([A-Za-z0-9_-]+)",'
    r'"preview":"(https://cdn\.hotscope\.tv/videos/\1/preview\.mp4)",'
    r'"caption":"(.*?)",'
    r'"thumbnailSD":"(.*?)",'
    r'"thumbnailHD":"(.*?)",'
    r'"duration":([0-9.]+),'
    r'"playlist":"(https://cdn\.hotscope\.tv/videos/\1/playlist\.m3u8)",'
    r'"createdAt":"(.*?)"',
    re.S,
)


@dataclass
class ListCard:
    id: str
    slug: str
    video_path: str
    preview_path: str  # HLS playlist (full video) — remuxed at upload
    poster_path: str
    title: str
    duration_seconds: int | None
    category: str


def _sleep() -> None:
    time.sleep(REQUEST_DELAY_SEC)


def _fetch(client: httpx.Client, url: str) -> str:
    _sleep()
    res = client.get(url, timeout=40.0)
    res.raise_for_status()
    return res.text


def video_id(slug: str) -> str:
    return f"{ID_PREFIX}{slug}"


def list_path(page: int = 1) -> str:
    if page <= 1:
        return "/trending"
    return f"/trending?page={page}"


def unescape_rsc(text: str) -> str:
    return text.replace('\\"', '"').replace("\\u0026", "&").replace("\\/", "/")


def parse_cards(html: str, *, category: str = "trending") -> list[ListCard]:
    text = unescape_rsc(html or "")
    cards: list[ListCard] = []
    seen: set[str] = set()
    for m in VIDEO_OBJ_RE.finditer(text):
        slug = m.group(1)
        vid = video_id(slug)
        if vid in seen:
            continue
        seen.add(vid)
        caption = m.group(3).encode("utf-8").decode("unicode_escape", errors="ignore")
        caption = re.sub(r"\s+", " ", caption).strip()
        duration = int(float(m.group(6)))
        # Prefer 720p variant playlist for ffmpeg remux.
        hls = f"{CDN}/videos/{slug}/video_1.m3u8"
        cards.append(
            ListCard(
                id=vid,
                slug=slug,
                video_path=f"/video/{slug}",
                preview_path=hls,
                poster_path=m.group(5) or m.group(4) or "",
                title=caption or slug,
                duration_seconds=duration if duration > 0 else None,
                category=category,
            )
        )
    return cards


def discover_last_page(client: httpx.Client, origin: str, *, hint: int = 800) -> int:
    """Binary search last non-empty /trending?page=N."""
    lo, hi = 1, max(hint * 2, 100)
    last = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        html = _fetch(client, abs_url(origin, list_path(mid)))
        n = len(parse_cards(html))
        if n > 0:
            last = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return last


def video_exists(conn: Any, vid: str) -> bool:
    row = conn.execute("SELECT 1 FROM videos WHERE id = %s", (vid,)).fetchone()
    return row is not None


def save_parsed(conn: Any, card: ListCard) -> None:
    conn.execute(
        """
        INSERT INTO videos (
          id, video_path, preview_path, poster_path, duration_seconds,
          title_ru, description_ru,
          title_en, description_en,
          category, category_en, tags, tags_en, status, parsed_at, updated_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'parsed', now(), now()
        )
        ON CONFLICT (id) DO NOTHING
        """,
        (
            card.id,
            card.video_path,
            card.preview_path,
            card.poster_path,
            card.duration_seconds,
            card.title,
            "",
            card.title,
            "",
            card.category,
            card.category,
            json.dumps([], ensure_ascii=False),
            json.dumps([], ensure_ascii=False),
        ),
    )


def _parser_mode() -> str:
    mode = os.environ.get("PARSER_MODE", "full").strip().lower()
    if mode not in ("latest", "backlog", "full"):
        raise ValueError(f"invalid PARSER_MODE: {mode!r}")
    return mode


def _ingest_card(conn: Any, card: ListCard) -> bool:
    if video_exists(conn, card.id):
        return False
    save_parsed(conn, card)
    print(f"parser: queued {card.id} {card.title[:60]!r}")
    return True


def _parse_latest(conn: Any, client: httpx.Client, origin: str, *, processed: int) -> int:
    """Fresh trending pages — keep queue current."""
    new_count = 0
    for page in (1, 2, 3):
        if processed >= MAX_VIDEOS or new_count >= MAX_NEW:
            break
        html = _fetch(client, abs_url(origin, list_path(page)))
        for card in parse_cards(html):
            if processed >= MAX_VIDEOS or new_count >= MAX_NEW:
                break
            if _ingest_card(conn, card):
                processed += 1
                new_count += 1
    return processed


def _load_backlog_page(conn: Any) -> int | None:
    raw = get_setting(conn, "hotscope.backlog_page", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _save_backlog_page(conn: Any, page: int, *, complete: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES ('hotscope.backlog_page', %s, now())
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
        """,
        (str(page),),
    )
    if complete:
        conn.execute(
            "UPDATE parser_state SET backlog_complete = true, updated_at = now() WHERE id = 1"
        )


def _parse_backlog(conn: Any, client: httpx.Client, origin: str, *, processed: int) -> int:
    """Walk from last page downward → archive without starving latest (pages 1–3)."""
    state = conn.execute("SELECT backlog_complete FROM parser_state WHERE id = 1").fetchone()
    if state and (state.get("backlog_complete") in (1, True, "1")):
        # reopen when new pages appear: rediscover last page occasionally
        last = discover_last_page(client, origin)
        cursor = _load_backlog_page(conn) or 4
        if last > cursor + 5:
            conn.execute(
                "UPDATE parser_state SET backlog_complete = false, updated_at = now() WHERE id = 1"
            )
            _save_backlog_page(conn, last)
            print(f"parser: hotscope backlog reopened last={last}")
        else:
            return processed

    page = _load_backlog_page(conn)
    if page is None:
        page = discover_last_page(client, origin)
        _save_backlog_page(conn, page)
        print(f"parser: hotscope backlog start from last page {page}")

    empty_streak = 0
    for _ in range(MAX_BACKLOG_PAGES):
        if processed >= MAX_VIDEOS:
            break
        if page < 4:
            _save_backlog_page(conn, page, complete=True)
            print("parser: hotscope backlog reached page 3 — complete")
            break

        html = _fetch(client, abs_url(origin, list_path(page)))
        cards = parse_cards(html)
        before = processed
        for card in cards:
            if processed >= MAX_VIDEOS:
                break
            if _ingest_card(conn, card):
                processed += 1

        if processed == before:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                page -= 1
                empty_streak = 0
                _save_backlog_page(conn, page)
                continue
        else:
            empty_streak = 0

        page -= 1
        _save_backlog_page(conn, page)
    return processed


def run_parser() -> int:
    mode = _parser_mode()
    processed = 0
    with db_conn() as conn:
        if not parser_ingest_allowed(conn):
            return 0
        origin = get_setting(conn, "hotscope.source_origin", ORIGIN_DEFAULT).rstrip("/")
        conn.execute(
            """
            UPDATE parser_state
            SET source_origin = %s, updated_at = now()
            WHERE id = 1
            """,
            (origin,),
        )
        with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
            if mode in ("latest", "full"):
                processed = _parse_latest(conn, client, origin, processed=processed)
            if mode in ("backlog", "full"):
                processed = _parse_backlog(conn, client, origin, processed=processed)
        conn.commit()
    print(f"parser[{mode}]: processed {processed} hotscope video(s)")
    return processed


if __name__ == "__main__":
    run_parser()

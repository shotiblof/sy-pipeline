"""strip2.co catalog parser — /videocat/new/ feed + pN pagination."""
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

SITE = "https://vps402.strip2.co"
ORIGIN_DEFAULT = SITE
ID_PREFIX = "s2"
LIST_BASE = "/videocat/new"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": f"{SITE}/",
}

# Listing card: /video/ID/?…> Title </a>
CARD_RE = re.compile(
    r'href="(?:https?://[^"/]+)?(/video/(\d+)/\?[^"]*)"[^>]*>\s*([^<]{3,200}?)\s*</a>',
    re.I,
)
PGN_TOTAL_RE = re.compile(
    r'class="pgn"[^>]*data-total="(\d+)"[^>]*data-on_page="(\d+)"',
    re.I,
)
POSTER_RE_TMPL = (
    r'(https://[^"\'\s>]+/{vid}\.v\.(?:200\.139|400\.278|600\.600|800\.800)[^"\'\s>]*\.jpg)'
)
ISO_DURATION_RE = re.compile(
    r'itemprop=["\']duration["\'][^>]*content=["\']PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?["\']',
    re.I,
)
# Prefer direct CDN file.mp4 (x0..x3). Fall back to site download links.
MP4_CDN_RE = re.compile(
    r'(https://fp\.spac\.me/[^"\'\s>]+/x(\d)/[^"\'\s>]+/file\.mp4)',
    re.I,
)
MP4_ANY_RE = re.compile(r'(https?://[^"\'\s>]+\.mp4[^"\'\s>]*)', re.I)


@dataclass
class ListCard:
    id: str
    numeric_id: str
    video_path: str
    title: str
    poster_path: str
    duration_seconds: int | None
    category: str


def _sleep() -> None:
    time.sleep(REQUEST_DELAY_SEC)


def _fetch(client: httpx.Client, url: str) -> str:
    _sleep()
    res = client.get(url, timeout=40.0)
    res.raise_for_status()
    return res.text


def video_id(numeric: str | int) -> str:
    return f"{ID_PREFIX}{numeric}"


def list_path(page: int = 1) -> str:
    if page <= 1:
        return f"{LIST_BASE}/"
    return f"{LIST_BASE}/p{page}/"


def parse_last_page(html: str) -> int:
    match = PGN_TOTAL_RE.search(html or "")
    if match:
        return max(1, int(match.group(1)))
    return 1


def _poster_for(html: str, numeric: str) -> str:
    pat = re.compile(POSTER_RE_TMPL.format(vid=re.escape(numeric)), re.I)
    hits = pat.findall(html)
    if not hits:
        return ""
    # Prefer small listing thumb if present.
    for url in hits:
        if ".200.139." in url:
            return url
    return hits[0]


def parse_cards(html: str, *, category: str = "new") -> list[ListCard]:
    cards: list[ListCard] = []
    seen: set[str] = set()
    for href, numeric, title in CARD_RE.findall(html or ""):
        vid = video_id(numeric)
        if vid in seen:
            continue
        seen.add(vid)
        title_clean = re.sub(r"\s+", " ", (title or "").strip())
        # Canonical watch path without tracking query — signed mp4 resolved at upload.
        video_path = f"/video/{numeric}/"
        cards.append(
            ListCard(
                id=vid,
                numeric_id=str(numeric),
                video_path=video_path,
                title=title_clean,
                poster_path=_poster_for(html, str(numeric)),
                duration_seconds=None,
                category=category,
            )
        )
    return cards


def parse_iso_duration(html: str) -> int | None:
    match = ISO_DURATION_RE.search(html or "")
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def extract_mp4_url(page_html: str) -> str | None:
    """Pick best direct mp4. Prefer CDN x0 (smallest/playable), else any .mp4."""
    cdn = MP4_CDN_RE.findall(page_html or "")
    if cdn:
        # sort by quality tier ascending → x0 first
        cdn_sorted = sorted(cdn, key=lambda t: int(t[1]))
        return cdn_sorted[0][0]
    for url in MP4_ANY_RE.findall(page_html or ""):
        if "favicon" in url.lower():
            continue
        return url
    return None


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
            "",  # signed mp4 resolved at upload from video page
            card.poster_path,
            card.duration_seconds,
            card.title,
            "",
            "",
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
    """Fresh pages only — new/ p1 (+p2) so queue stays current without digging archive."""
    new_count = 0
    for page in (1, 2):
        if processed >= MAX_VIDEOS or new_count >= MAX_NEW:
            break
        html = _fetch(client, abs_url(origin, list_path(page)))
        for card in parse_cards(html, category="new"):
            if processed >= MAX_VIDEOS or new_count >= MAX_NEW:
                break
            if _ingest_card(conn, card):
                processed += 1
                new_count += 1
    return processed


def _load_backlog_page(conn: Any) -> int:
    raw = get_setting(conn, "strip2.backlog_page", "3")
    try:
        page = int(raw)
    except ValueError:
        page = 3
    return max(3, page)


def _save_backlog_page(conn: Any, page: int, *, complete: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES ('strip2.backlog_page', %s, now())
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
        """,
        (str(page),),
    )
    if complete:
        conn.execute(
            "UPDATE parser_state SET backlog_complete = true, updated_at = now() WHERE id = 1"
        )


def _parse_backlog(conn: Any, client: httpx.Client, origin: str, *, processed: int) -> int:
    state = conn.execute("SELECT backlog_complete FROM parser_state WHERE id = 1").fetchone()
    if state and (state.get("backlog_complete") in (1, True, "1")):
        return processed

    page = _load_backlog_page(conn)
    empty_streak = 0
    last_page = None
    for _ in range(MAX_BACKLOG_PAGES):
        if processed >= MAX_VIDEOS:
            break
        html = _fetch(client, abs_url(origin, list_path(page)))
        if last_page is None:
            last_page = parse_last_page(html)
        cards = parse_cards(html, category="new")
        before = processed
        for card in cards:
            if processed >= MAX_VIDEOS:
                break
            if _ingest_card(conn, card):
                processed += 1

        if processed == before:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                _save_backlog_page(conn, page + 1, complete=page + 1 > (last_page or page))
                if page >= (last_page or page):
                    print("parser: strip2 backlog complete")
                    break
        else:
            empty_streak = 0

        page += 1
        if last_page and page > last_page:
            _save_backlog_page(conn, page, complete=True)
            print("parser: strip2 backlog complete")
            break
        _save_backlog_page(conn, page)
    return processed


def run_parser() -> int:
    mode = _parser_mode()
    processed = 0
    with db_conn() as conn:
        if not parser_ingest_allowed(conn):
            return 0
        origin = get_setting(conn, "strip2.source_origin", ORIGIN_DEFAULT).rstrip("/")
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
    print(f"parser[{mode}]: processed {processed} strip2 video(s)")
    return processed


if __name__ == "__main__":
    run_parser()

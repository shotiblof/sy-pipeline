"""sy hybrid catalog parser — strip2.co + hotscope.tv/trending into one queue.

Uses existing strip2/hotscope card parsers. Source-specific backlog cursors
live in app_settings so the two feeds never clobber each other.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from peach_db import abs_url, db_conn, get_setting
from pipeline_config import MAX_BACKLOG_PAGES, MAX_EMPTY_PAGES, MAX_NEW, MAX_VIDEOS, REQUEST_DELAY_SEC
from queue_guard import parser_ingest_allowed

import hotscope_parser as hs
import strip2_parser as s2

# Split per-run budget across both sources (latest first, then backlog fill).
_SPLIT = max(1, MAX_VIDEOS // 2)


def _parser_mode() -> str:
    mode = os.environ.get("PARSER_MODE", "full").strip().lower()
    if mode not in ("latest", "backlog", "full"):
        raise ValueError(f"invalid PARSER_MODE: {mode!r}")
    return mode


def _setting_bool(conn: Any, key: str) -> bool:
    raw = (get_setting(conn, key, "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _set_setting(conn: Any, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
        """,
        (key, value),
    )


def _ingest_s2(conn: Any, card: s2.ListCard) -> bool:
    if s2.video_exists(conn, card.id):
        return False
    s2.save_parsed(conn, card)
    print(f"parser: queued {card.id} strip2 {card.title[:60]!r}")
    return True


def _ingest_hs(conn: Any, card: hs.ListCard) -> bool:
    if hs.video_exists(conn, card.id):
        return False
    hs.save_parsed(conn, card)
    print(f"parser: queued {card.id} hotscope {card.title[:60]!r}")
    return True


def _s2_latest(conn: Any, client: httpx.Client, origin: str, *, budget: int, processed: int) -> int:
    new_count = 0
    for page in (1, 2):
        if processed >= budget or new_count >= MAX_NEW:
            break
        html = s2._fetch(client, abs_url(origin, s2.list_path(page)))
        for card in s2.parse_cards(html, category="new"):
            if processed >= budget or new_count >= MAX_NEW:
                break
            if _ingest_s2(conn, card):
                processed += 1
                new_count += 1
    return processed


def _s2_backlog(conn: Any, client: httpx.Client, origin: str, *, budget: int, processed: int) -> int:
    if _setting_bool(conn, "sy.strip2.backlog_complete"):
        return processed

    raw = get_setting(conn, "sy.strip2.backlog_page", "3")
    try:
        page = max(3, int(raw))
    except ValueError:
        page = 3

    empty_streak = 0
    last_page = None
    for _ in range(MAX_BACKLOG_PAGES):
        if processed >= budget:
            break
        html = s2._fetch(client, abs_url(origin, s2.list_path(page)))
        if last_page is None:
            last_page = s2.parse_last_page(html)
        before = processed
        for card in s2.parse_cards(html, category="new"):
            if processed >= budget:
                break
            if _ingest_s2(conn, card):
                processed += 1

        if processed == before:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                page += 1
                _set_setting(conn, "sy.strip2.backlog_page", str(page))
                if last_page and page > last_page:
                    _set_setting(conn, "sy.strip2.backlog_complete", "1")
                    print("parser: sy strip2 backlog complete")
                    break
                empty_streak = 0
                continue
        else:
            empty_streak = 0

        page += 1
        _set_setting(conn, "sy.strip2.backlog_page", str(page))
        if last_page and page > last_page:
            _set_setting(conn, "sy.strip2.backlog_complete", "1")
            print("parser: sy strip2 backlog complete")
            break
    return processed


def _hs_latest(conn: Any, client: httpx.Client, origin: str, *, budget: int, processed: int) -> int:
    new_count = 0
    for page in (1, 2, 3):
        if processed >= budget or new_count >= MAX_NEW:
            break
        html = hs._fetch(client, abs_url(origin, hs.list_path(page)))
        for card in hs.parse_cards(html):
            if processed >= budget or new_count >= MAX_NEW:
                break
            if _ingest_hs(conn, card):
                processed += 1
                new_count += 1
    return processed


def _hs_backlog(conn: Any, client: httpx.Client, origin: str, *, budget: int, processed: int) -> int:
    if _setting_bool(conn, "sy.hotscope.backlog_complete"):
        last = hs.discover_last_page(client, origin)
        raw = get_setting(conn, "sy.hotscope.backlog_page", "4")
        try:
            cursor = int(raw)
        except ValueError:
            cursor = 4
        if last > cursor + 5:
            _set_setting(conn, "sy.hotscope.backlog_complete", "0")
            _set_setting(conn, "sy.hotscope.backlog_page", str(last))
            print(f"parser: sy hotscope backlog reopened last={last}")
        else:
            return processed

    raw = get_setting(conn, "sy.hotscope.backlog_page", "").strip()
    if not raw:
        page = hs.discover_last_page(client, origin)
        _set_setting(conn, "sy.hotscope.backlog_page", str(page))
        print(f"parser: sy hotscope backlog start from last page {page}")
    else:
        try:
            page = int(raw)
        except ValueError:
            page = hs.discover_last_page(client, origin)
            _set_setting(conn, "sy.hotscope.backlog_page", str(page))

    empty_streak = 0
    for _ in range(MAX_BACKLOG_PAGES):
        if processed >= budget:
            break
        if page < 4:
            _set_setting(conn, "sy.hotscope.backlog_complete", "1")
            print("parser: sy hotscope backlog reached page 3 — complete")
            break

        html = hs._fetch(client, abs_url(origin, hs.list_path(page)))
        before = processed
        for card in hs.parse_cards(html):
            if processed >= budget:
                break
            if _ingest_hs(conn, card):
                processed += 1

        if processed == before:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                page -= 1
                empty_streak = 0
                _set_setting(conn, "sy.hotscope.backlog_page", str(page))
                continue
        else:
            empty_streak = 0

        page -= 1
        _set_setting(conn, "sy.hotscope.backlog_page", str(page))
    return processed


def _retire_old_seksyukle(conn: Any) -> int:
    """Old sy* seksyukle rows must not block the hybrid queue_guard."""
    cur = conn.execute(
        """
        UPDATE videos
        SET status = 'skipped',
            error_message = 'retired: sy switched to strip2+hotscope hybrid',
            updated_at = now()
        WHERE status = 'parsed'
          AND id LIKE 'sy%%'
        RETURNING id
        """
    )
    rows = cur.fetchall()
    n = len(rows)
    if n:
        print(f"parser: retired {n} old seksyukle row(s)")
    return n


def run_parser() -> int:
    mode = _parser_mode()
    processed = 0
    with db_conn() as conn:
        _retire_old_seksyukle(conn)
        if not parser_ingest_allowed(conn):
            conn.commit()
            return 0

        s2_origin = get_setting(conn, "strip2.source_origin", s2.ORIGIN_DEFAULT).rstrip("/")
        hs_origin = get_setting(conn, "hotscope.source_origin", hs.ORIGIN_DEFAULT).rstrip("/")
        conn.execute(
            """
            UPDATE parser_state
            SET source_origin = %s, updated_at = now()
            WHERE id = 1
            """,
            (f"hybrid:{s2_origin}+{hs_origin}",),
        )

        s2_budget = _SPLIT

        with httpx.Client(headers=s2.HEADERS, follow_redirects=True) as s2_client:
            s2_done = 0
            if mode in ("latest", "full"):
                s2_done = _s2_latest(conn, s2_client, s2_origin, budget=s2_budget, processed=0)
            if mode in ("backlog", "full") and s2_done < s2_budget:
                s2_done = _s2_backlog(
                    conn, s2_client, s2_origin, budget=s2_budget, processed=s2_done
                )
            processed += s2_done

        hs_cap = max(0, MAX_VIDEOS - processed)
        with httpx.Client(headers=hs.HEADERS, follow_redirects=True) as hs_client:
            hs_done = 0
            if hs_cap > 0 and mode in ("latest", "full"):
                hs_done = _hs_latest(conn, hs_client, hs_origin, budget=hs_cap, processed=0)
            if hs_cap > 0 and mode in ("backlog", "full") and hs_done < hs_cap:
                hs_done = _hs_backlog(
                    conn, hs_client, hs_origin, budget=hs_cap, processed=hs_done
                )
            processed += hs_done

        conn.commit()

    print(
        f"parser[hybrid/{mode}]: processed {processed} video(s) "
        f"(strip2+hotscope, delay={REQUEST_DELAY_SEC}s)"
    )
    return processed


if __name__ == "__main__":
    run_parser()

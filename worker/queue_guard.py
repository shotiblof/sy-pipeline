"""Upload queue health — rebalance stuck rows, backpressure helpers."""
from __future__ import annotations

import os
from typing import Any

from peach_db import db_conn


def parsed_queue_depth(conn) -> int:
    row = conn.execute(
        "SELECT count(*)::int AS n FROM videos WHERE status = 'parsed'"
    ).fetchone()
    return int(row["n"] if row else 0)


def max_parsed_queue() -> int:
    return int(os.environ.get("PARSER_MAX_PARSED_QUEUE", "120"))


def ebalka_parser_allowed(conn) -> bool:
    return parser_ingest_allowed(conn)


def parser_ingest_allowed(conn) -> bool:
    depth = parsed_queue_depth(conn)
    cap = max_parsed_queue()
    if depth >= cap:
        print(f"queue_guard: parsed backlog {depth} >= {cap} — skip parser ingest")
        return False
    return True


def rebalance_upload_queue(
    *,
    stuck_uploading_minutes: int = 20,
    hard_stuck_minutes: int = 75,
) -> dict[str, Any]:
    """Return stuck rows to parsed; clear stale reset markers. Does not delete parsed backlog."""
    stats: dict[str, Any] = {
        "immediate_uploading_reset": 0,
        "soft_uploading_reset": 0,
        "hard_uploading_reset": 0,
        "failed_reset": 0,
        "parsed_errors_cleared": 0,
    }

    with db_conn() as conn:
        cur = conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                error_message = NULL,
                updated_at = now()
            WHERE status = 'uploading'
              AND (pre_filecode IS NULL OR pre_filecode = '')
            RETURNING id
            """
        )
        stats["immediate_uploading_reset"] = len(cur.fetchall())

        cur = conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                pre_filecode = NULL,
                full_filecode = NULL,
                namevids_file_id = NULL,
                error_message = 'rebalance: upload timed out',
                updated_at = now()
            WHERE status = 'uploading'
              AND updated_at < now() - make_interval(mins => %s)
            RETURNING id
            """,
            (hard_stuck_minutes,),
        )
        stats["hard_uploading_reset"] = len(cur.fetchall())

        cur = conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                updated_at = now()
            WHERE status = 'failed'
              AND (pre_filecode IS NULL OR pre_filecode = '')
              AND NOT (
                error_message ILIKE '%%too small%%'
                OR error_message ILIKE '%%expired%%'
                OR error_message ILIKE '%%20 files%%'
                OR error_message ILIKE '%%too fast%%'
                OR error_message ILIKE '%%description_invalid%%'
                OR error_message ILIKE '%%title_invalid%%'
              )
            RETURNING id
            """
        )
        stats["failed_reset"] = len(cur.fetchall())

        cur = conn.execute(
            """
            UPDATE videos
            SET error_message = NULL,
                updated_at = now()
            WHERE status = 'parsed'
              AND error_message IS NOT NULL
              AND (
                error_message LIKE 'reset:%%'
                OR error_message LIKE 'rebalance:%%'
                OR error_message LIKE 'manual%%'
              )
            RETURNING id
            """
        )
        stats["parsed_errors_cleared"] = len(cur.fetchall())

        stats["parsed_depth"] = parsed_queue_depth(conn)
        stats["uploading"] = conn.execute(
            "SELECT count(*)::int AS n FROM videos WHERE status = 'uploading'"
        ).fetchone()["n"]
        conn.commit()

    print(f"queue_guard: rebalance {stats}")
    return stats


if __name__ == "__main__":
    rebalance_upload_queue()

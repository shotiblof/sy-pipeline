"""Apply pipeline SQLite schema and force seksyukle-only settings."""
from __future__ import annotations

from peach_db import SCHEMA_PATH, db_conn


def apply_migrations() -> None:
    if not SCHEMA_PATH.is_file():
        raise RuntimeError(f"missing schema: {SCHEMA_PATH}")
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, description, updated_at)
            VALUES (
              'parser.primary_source',
              'seksyukle',
              'Catalog source (seksyukle only)',
              datetime('now')
            )
            ON CONFLICT (key) DO UPDATE SET
              value = 'seksyukle',
              updated_at = datetime('now')
            """
        )
        conn.execute(
            """
            INSERT INTO app_settings (key, value, description, updated_at)
            VALUES (
              'seksyukle.source_origin',
              'https://seksyukle.org',
              'seksyukle.org origin',
              datetime('now')
            )
            ON CONFLICT (key) DO UPDATE SET
              value = 'https://seksyukle.org',
              updated_at = datetime('now')
            """
        )
        conn.execute(
            """
            INSERT INTO app_settings (key, value, description, updated_at)
            VALUES (
              'namevids.caption_link',
              'https://telegram.dog/lenomore',
              'sy pipeline caption TG channel',
              datetime('now')
            )
            ON CONFLICT (key) DO UPDATE SET
              value = 'https://telegram.dog/lenomore',
              description = 'sy pipeline caption TG channel',
              updated_at = datetime('now')
            """
        )
        row = conn.execute(
            "SELECT count(*) AS n FROM sqlite_master WHERE type='table'"
        ).fetchone()
        print(f"migrations: sqlite schema ready ({(row or {}).get('n', 0)} tables), source=seksyukle")
        conn.commit()


if __name__ == "__main__":
    apply_migrations()

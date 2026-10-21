"""Peach pipeline DB — SQLite / libSQL (Turso) or GitHub-hosted SQLite file.

Priority:
1. TURSO_DATABASE_URL + TURSO_AUTH_TOKEN → remote Turso/libSQL
2. PIPELINE_SQLITE_PATH → local file
3. PIPELINE_DB_GITHUB_REPO (+ SHOTIBLOF_GITHUB_TOKEN) → clone/pull SQLite from GitHub
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import libsql

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "db" / "pipeline_schema.sqlite.sql"
DEFAULT_GITHUB_REPO = "shotiblof/peach-pipeline-db"
DEFAULT_DB_NAME = "pipeline.db"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def get_database_url() -> str:
    """Legacy name — returns Turso URL or local sqlite path for diagnostics."""
    turso = _env("TURSO_DATABASE_URL")
    if turso:
        return turso
    path = _env("PIPELINE_SQLITE_PATH")
    if path:
        return path
    return f"github:{_env('PIPELINE_DB_GITHUB_REPO', DEFAULT_GITHUB_REPO)}"


class _DictRow(dict):
    """dict that also allows attribute-style access used sparsely in workers."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


class _Result:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.description = getattr(cursor, "description", None)

    def _row(self, raw: Any) -> _DictRow | None:
        if raw is None:
            return None
        if isinstance(raw, dict):
            return _DictRow(raw)
        cols = [d[0] for d in (self.description or [])]
        if not cols:
            return _DictRow({"value": raw[0]}) if raw else None
        return _DictRow(zip(cols, raw))

    def fetchone(self) -> _DictRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[_DictRow]:
        rows = self._cursor.fetchall() or []
        out: list[_DictRow] = []
        for raw in rows:
            mapped = self._row(raw)
            if mapped is not None:
                out.append(mapped)
        return out

    def __iter__(self) -> Iterator[_DictRow]:
        return iter(self.fetchall())


def _expand_any(sql: str, params: tuple[Any, ...] | list[Any] | None) -> tuple[str, tuple[Any, ...]]:
    if not params:
        return sql, ()
    params_list = list(params)
    while True:
        match = re.search(r"=?\s*ANY\s*\(\s*\?\s*\)", sql, flags=re.I)
        if not match:
            break
        # find which ? index this is
        before = sql[: match.start()]
        q_index = before.count("?")
        if q_index >= len(params_list):
            raise RuntimeError(f"ANY(?) param missing in SQL: {sql}")
        values = params_list[q_index]
        if not isinstance(values, (list, tuple)):
            raise RuntimeError("ANY(?) expects a list/tuple param")
        if not values:
            placeholders = "NULL"
            repl_params: list[Any] = []
        else:
            placeholders = ", ".join("?" for _ in values)
            repl_params = list(values)
        # Keep equality: id = ANY(?) → id IN (...)
        fragment = match.group(0)
        if "=" in fragment:
            sql = sql[: match.start()] + f" IN ({placeholders})" + sql[match.end() :]
        else:
            sql = sql[: match.start()] + f"({placeholders})" + sql[match.end() :]
        params_list = params_list[:q_index] + repl_params + params_list[q_index + 1 :]
    return sql, tuple(params_list)


def to_sqlite_sql(sql: str) -> str:
    """Best-effort Postgres → SQLite rewrite for pipeline queries."""
    out = sql
    out = out.replace("%s", "?")
    out = out.replace("%%", "%")
    out = re.sub(r"::jsonb\b", "", out, flags=re.I)
    out = re.sub(r"::int\b", "", out, flags=re.I)
    out = re.sub(r"::text\b", "", out, flags=re.I)
    out = re.sub(r"\bILIKE\b", "LIKE", out, flags=re.I)
    out = re.sub(r"\bTRUE\b", "1", out, flags=re.I)
    out = re.sub(r"\bFALSE\b", "0", out, flags=re.I)
    out = re.sub(r"\bnow\(\)", "datetime('now')", out, flags=re.I)
    out = re.sub(
        r"\binterval\s+'(\d+)\s+(minutes|hours|days)'",
        r"'\1 \2'",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"datetime\('now'\)\s*-\s*'(\d+ (?:minutes|hours|days))'",
        r"datetime('now', '-\1')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"datetime\('now'\)\s*\+\s*'(\d+ (?:minutes|hours|days))'",
        r"datetime('now', '+\1')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"make_interval\s*\(\s*mins\s*=>\s*\?\s*\)",
        "('-' || ? || ' minutes')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"make_interval\s*\(\s*days\s*=>\s*\?\s*\)",
        "('-' || ? || ' days')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"datetime\('now'\)\s*-\s*\('-'\s*\|\|\s*\?\s*\|\|\s*' minutes'\)",
        "datetime('now', '-' || ? || ' minutes')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"datetime\('now'\)\s*-\s*\('-'\s*\|\|\s*\?\s*\|\|\s*' days'\)",
        "datetime('now', '-' || ? || ' days')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"datetime\('now'\)\s*\+\s*\('-'\s*\|\|\s*\?\s*\|\|\s*' days'\)",
        "datetime('now', '+' || ? || ' days')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"date_trunc\s*\(\s*'day'\s*,\s*datetime\('now'\)\s*AT TIME ZONE\s*'UTC'\s*\)",
        "date('now')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"date_trunc\s*\(\s*'day'\s*,\s*now\(\)\s*AT TIME ZONE\s*'UTC'\s*\)",
        "date('now')",
        out,
        flags=re.I,
    )
    out = re.sub(r"\s+FOR UPDATE SKIP LOCKED\b", "", out, flags=re.I)
    out = re.sub(r"\s+FOR UPDATE\b", "", out, flags=re.I)
    out = re.sub(r"\s+NULLS LAST\b", "", out, flags=re.I)
    out = re.sub(r"\s+NULLS FIRST\b", "", out, flags=re.I)
    # COALESCE stays — SQLite supports it
    # Postgres case-insensitive regex: col ~* 'pat' → LOWER(col) LIKE '%pat%'
    # (patterns with | are not expanded; prefer portable LIKE in callers)
    out = re.sub(
        r"(\w+(?:\.\w+)?)\s*~\*\s*'([^'|]+)'",
        r"(LOWER(COALESCE(\1, '')) LIKE '%' || LOWER('\2') || '%')",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"COALESCE\(([^,]+),\s*''\)\s*~\*\s*'([^'|]+)'",
        r"(LOWER(COALESCE(\1, '')) LIKE '%' || LOWER('\2') || '%')",
        out,
        flags=re.I,
    )
    return out


class PipelineConnection:
    def __init__(self, raw: Any, *, on_commit: Any | None = None) -> None:
        self._raw = raw
        self._on_commit = on_commit
        self._dirty = False

    def execute(self, sql: str, params: Any = None) -> _Result:
        if params is None:
            params_tuple: tuple[Any, ...] = ()
        elif isinstance(params, tuple):
            params_tuple = params
        elif isinstance(params, list):
            params_tuple = tuple(params)
        else:
            params_tuple = (params,)

        # JSON dumps for dict/list metadata columns when callers pass objects
        coerced: list[Any] = []
        for value in params_tuple:
            if isinstance(value, (dict, list)):
                coerced.append(json.dumps(value, ensure_ascii=False))
            elif isinstance(value, bool):
                coerced.append(1 if value else 0)
            else:
                coerced.append(value)
        params_tuple = tuple(coerced)

        sqlite_sql = to_sqlite_sql(sql)
        sqlite_sql, params_tuple = _expand_any(sqlite_sql, params_tuple)
        cursor = self._raw.execute(sqlite_sql, params_tuple)
        if re.match(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", sqlite_sql, flags=re.I):
            self._dirty = True
        return _Result(cursor)

    def executemany(self, sql: str, seq_of_params: list[Any]) -> None:
        sqlite_sql = to_sqlite_sql(sql)
        self._raw.executemany(sqlite_sql, seq_of_params)
        self._dirty = True

    def executescript(self, script: str) -> None:
        self._raw.executescript(script)
        self._dirty = True

    def commit(self) -> None:
        self._raw.commit()
        if self._dirty and self._on_commit:
            self._on_commit()
        self._dirty = False

    def rollback(self) -> None:
        self._raw.rollback()
        self._dirty = False

    def close(self) -> None:
        self._raw.close()


def _ensure_schema(conn: Any) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()


def _github_token() -> str:
    return (
        _env("SHOTIBLOF_GITHUB_TOKEN")
        or _env("GITHUB_TOKEN")
        or _env("GH_TOKEN")
    )


def _github_repo() -> str:
    return _env("PIPELINE_DB_GITHUB_REPO", DEFAULT_GITHUB_REPO)


def _github_db_dir() -> Path:
    override = _env("PIPELINE_DB_LOCAL_DIR")
    if override:
        return Path(override)
    repo = _github_repo().replace("/", "-") or "pipeline-db"
    return Path(tempfile.gettempdir()) / repo


def _git_env(token: str) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GH_TOKEN": token,
    }


def _sync_github_db() -> Path:
    """Clone or pull private GitHub sqlite store; return path to pipeline.db."""
    token = _github_token()
    if not token:
        raise RuntimeError("SHOTIBLOF_GITHUB_TOKEN required for GitHub SQLite backend")
    repo = _github_repo()
    dest = _github_db_dir()
    db_path = dest / DEFAULT_DB_NAME
    remote = f"https://x-access-token:{token}@github.com/{repo}.git"
    env = _git_env(token)

    if (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "origin"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(dest), "reset", "--hard", "origin/main"],
            check=False,
            env=env,
        )
        # if main missing, try master
        head = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            env=env,
        )
        branch = (head.stdout or "main").strip() or "main"
        subprocess.run(
            ["git", "-C", str(dest), "reset", "--hard", f"origin/{branch}"],
            check=False,
            env=env,
        )
    else:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", remote, str(dest)],
            capture_output=True,
            text=True,
            env=env,
        )
        if clone.returncode != 0:
            # empty / missing repo — init locally; publisher creates remote later
            subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, env=env)
            subprocess.run(
                ["git", "remote", "add", "origin", remote],
                cwd=dest,
                check=False,
                env=env,
            )

    if not db_path.exists():
        raw = libsql.connect(str(db_path))
        _ensure_schema(raw)
        raw.close()

    return db_path


def _push_github_db(db_path: Path) -> None:
    token = _github_token()
    if not token:
        return
    dest = db_path.parent
    env = _git_env(token)
    env.update(
        {
            "GIT_AUTHOR_NAME": "peach-pipeline",
            "GIT_AUTHOR_EMAIL": "225040275+shotiblof@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "peach-pipeline",
            "GIT_COMMITTER_EMAIL": "225040275+shotiblof@users.noreply.github.com",
        }
    )
    subprocess.run(["git", "-C", str(dest), "add", "-f", DEFAULT_DB_NAME], check=False, env=env)
    status = subprocess.run(
        ["git", "-C", str(dest), "status", "--porcelain", DEFAULT_DB_NAME],
        capture_output=True,
        text=True,
        env=env,
    )
    if not (status.stdout or "").strip():
        print("peach_db: github db unchanged, skip push")
        return
    commit = subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "sync pipeline db"],
        capture_output=True,
        text=True,
        env=env,
    )
    if commit.returncode != 0:
        print(f"peach_db: github commit failed: {(commit.stderr or commit.stdout)[:400]}")
        return
    push = subprocess.run(
        ["git", "-C", str(dest), "push", "-u", "origin", "HEAD"],
        capture_output=True,
        text=True,
        env=env,
    )
    if push.returncode != 0:
        print(f"peach_db: github push warning: {(push.stderr or push.stdout)[:400]}")
    else:
        print("peach_db: github db pushed")


def _open_raw() -> tuple[Any, Any | None, bool]:
    """Return (raw_conn, on_commit_callback, owns_schema_init)."""
    turso = _env("TURSO_DATABASE_URL")
    token = _env("TURSO_AUTH_TOKEN")
    if turso:
        raw = libsql.connect(database=turso, auth_token=token or None)
        return raw, None, True

    local = _env("PIPELINE_SQLITE_PATH")
    if local:
        path = Path(local)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = libsql.connect(str(path))
        return raw, None, True

    # Default free backend: GitHub-hosted sqlite
    db_path = _sync_github_db()
    raw = libsql.connect(str(db_path))

    def on_commit() -> None:
        _push_github_db(db_path)

    return raw, on_commit, True


@contextmanager
def db_conn() -> Iterator[PipelineConnection]:
    raw, on_commit, init_schema = _open_raw()
    conn = PipelineConnection(raw, on_commit=on_commit)
    try:
        if init_schema:
            _ensure_schema(raw)
        yield conn
        if conn._dirty:
            conn.commit()
    finally:
        try:
            raw.close()
        except Exception:
            pass


def get_setting(conn: PipelineConnection, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    if not row:
        return default
    return str(row["value"] or default)


def get_upload_account(conn: PipelineConnection, provider: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, provider, name, login, secret, metadata
        FROM upload_accounts
        WHERE provider = ? AND is_enabled = 1
        ORDER BY priority ASC, id ASC
        LIMIT 1
        """,
        (provider,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    meta = data.get("metadata")
    if isinstance(meta, str) and meta.strip():
        try:
            data["metadata"] = json.loads(meta)
        except json.JSONDecodeError:
            data["metadata"] = {}
    elif not meta:
        data["metadata"] = {}
    return data


def abs_url(origin: str, path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return origin.rstrip("/") + "/" + path.lstrip("/")


def to_storage_path(url: str, origin: str) -> str:
    if url.startswith("/"):
        return url
    parsed = urlparse(url)
    origin_host = urlparse(origin).netloc
    if parsed.netloc == origin_host:
        query = f"?{parsed.query}" if parsed.query else ""
        return parsed.path + query
    return url


def tags_to_hashtags(tags: list[str] | Any, limit: int = 5) -> str:
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            return ""
    if not isinstance(tags, list):
        return ""
    parts: list[str] = []
    for tag in tags[:limit]:
        if not isinstance(tag, str) or not tag.strip():
            continue
        slug = tag.strip().replace(" ", "_")
        parts.append(f"#{slug}")
    return " ".join(parts)

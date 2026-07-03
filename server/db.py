"""SQLite によるジョブ・文字起こし結果の永続化。

v1 では SQLite で十分。社内公開時に PostgreSQL 等へ載せ替える場合は
このモジュールのインターフェースを保ったまま実装を差し替える。
"""

import sqlite3
import time
import uuid
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL,           -- queued / processing / done / error
    error       TEXT,
    language    TEXT,
    vocabulary  TEXT,                    -- 用語リスト (hotwords)
    context     TEXT,                    -- 前提コンテキスト (initial_prompt)
    duration    REAL,                    -- 音声全体の長さ(秒)
    progress    REAL NOT NULL DEFAULT 0, -- 0.0〜1.0
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    job_id  TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    start   REAL NOT NULL,
    end     REAL NOT NULL,
    text    TEXT NOT NULL,
    PRIMARY KEY (job_id, idx)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        # 既存 DB へのカラム追加(v1 からのマイグレーション)
        for column in ("vocabulary", "context"):
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass  # 既に存在する


def create_job(
    filename: str,
    language: str | None,
    vocabulary: str | None = None,
    context: str | None = None,
) -> str:
    job_id = uuid.uuid4().hex
    with _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, filename, status, language, vocabulary, context, created_at)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?)",
            (job_id, filename, language, vocabulary, context, time.time()),
        )
    return job_id


def set_status(job_id: str, status: str, error: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, error = ? WHERE id = ?",
            (status, error, job_id),
        )


def set_duration(job_id: str, duration: float | None) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET duration = ? WHERE id = ?", (duration, job_id))


def set_language(job_id: str, language: str | None) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET language = ? WHERE id = ?", (language, job_id))


def set_progress(job_id: str, progress: float) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE id = ?",
            (min(max(progress, 0.0), 1.0), job_id),
        )


def add_segment(job_id: str, idx: int, start: float, end: float, text: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO segments (job_id, idx, start, end, text) VALUES (?, ?, ?, ?, ?)",
            (job_id, idx, start, end, text),
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_segments(job_id: str, offset: int = 0) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT idx, start, end, text FROM segments WHERE job_id = ? AND idx >= ? ORDER BY idx",
            (job_id, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_job(job_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

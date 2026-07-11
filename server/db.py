"""SQLite によるジョブ・文字起こし結果・プリセットの永続化。

テーブル構成(スキーマは _SCHEMA、詳細は docs/architecture.md):
  jobs      : ファイル文字起こしのジョブ(状態・進捗・設定・話者氏名)
  segments  : 文字起こし結果のセグメント(時刻・テキスト・話者番号)
  presets   : 用語リスト・コンテキストの共有プリセット
  summaries : LLM で生成した要約・議事録(ジョブ×種類ごとに1件、再生成で上書き)

接続は操作ごとに開閉するシンプルな方式(WAL モード)。書き込み頻度は
セグメント確定時程度なので、この規模ではコネクションプール等は不要。
スキーマ変更時は init_db() のマイグレーション(ALTER TABLE)に追記する。

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
    status      TEXT NOT NULL,           -- queued / processing / done / error / canceled
    error       TEXT,
    language    TEXT,
    vocabulary  TEXT,                    -- 用語リスト (hotwords)
    context     TEXT,                    -- 前提コンテキスト (initial_prompt)
    speaker_names TEXT,                  -- 話者番号→氏名の JSON マップ
    min_speakers INTEGER,                -- 話者の人数の下限(利用者申告、未指定は NULL)
    max_speakers INTEGER,                -- 話者の人数の上限(同上。下限と同数なら固定)
    duration    REAL,                    -- 音声全体の長さ(秒)
    progress    REAL NOT NULL DEFAULT 0, -- 0.0〜1.0
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS presets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,    -- プリセット名(会議名・案件名など)
    vocabulary  TEXT NOT NULL DEFAULT '',
    context     TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    job_id  TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    start   REAL NOT NULL,
    end     REAL NOT NULL,
    text    TEXT NOT NULL,
    speaker INTEGER,                     -- 話者番号 (1始まり、無効時は NULL)
    PRIMARY KEY (job_id, idx)
);
CREATE TABLE IF NOT EXISTS summaries (
    job_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- summary(要約) / minutes(議事録)
    content    TEXT NOT NULL,
    model      TEXT NOT NULL,            -- 生成に使った LLM モデル名
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, kind)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """テーブル作成と過去バージョンからのマイグレーションを行う(起動時に呼ぶ)。"""
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        # 既存 DB へのカラム追加(過去バージョンからのマイグレーション)
        for table, column, type_ in (
            ("jobs", "vocabulary", "TEXT"),
            ("jobs", "context", "TEXT"),
            ("jobs", "speaker_names", "TEXT"),
            ("jobs", "min_speakers", "INTEGER"),
            ("jobs", "max_speakers", "INTEGER"),
            ("segments", "speaker", "INTEGER"),
        ):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
            except sqlite3.OperationalError:
                pass  # 既に存在する


def create_job(
    filename: str,
    language: str | None,
    vocabulary: str | None = None,
    context: str | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> str:
    """queued 状態のジョブを作成してジョブ ID を返す。"""
    job_id = uuid.uuid4().hex
    with _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, filename, status, language, vocabulary, context,"
            " min_speakers, max_speakers, created_at)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                filename,
                language,
                vocabulary,
                context,
                min_speakers,
                max_speakers,
                time.time(),
            ),
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


def add_segment(
    job_id: str,
    idx: int,
    start: float,
    end: float,
    text: str,
    speaker: int | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO segments (job_id, idx, start, end, text, speaker)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, idx, start, end, text, speaker),
        )


def update_segment_speakers(job_id: str, speakers: dict[int, int]) -> None:
    """セグメントの話者番号を一括で置き換える(完了時の一括話者分離用)。"""
    with _conn() as conn:
        conn.executemany(
            "UPDATE segments SET speaker = ? WHERE job_id = ? AND idx = ?",
            [(spk, job_id, idx) for idx, spk in speakers.items()],
        )


def set_speaker_names(job_id: str, names_json: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET speaker_names = ? WHERE id = ?", (names_json, job_id)
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_segments(job_id: str, offset: int = 0) -> list[dict[str, Any]]:
    """セグメントを idx 順に返す。offset 以降だけ返せるので差分ポーリングに使える。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT idx, start, end, text, speaker FROM segments WHERE job_id = ? AND idx >= ? ORDER BY idx",
            (job_id, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def list_jobs_by_status(statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """指定した状態のジョブを古い順に返す(再起動時の回収用)。"""
    placeholders = ",".join("?" for _ in statuses)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at",
            statuses,
        ).fetchall()
        return [dict(r) for r in rows]


def reset_job(job_id: str) -> None:
    """ジョブを queued に戻し、途中結果(セグメント・進捗)を消す。

    processing 中にプロセスが落ちたジョブを最初からやり直すために使う。
    Whisper は途中から再開できないため、途中結果は破棄して作り直す。
    """
    with _conn() as conn:
        conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
        conn.execute(
            "UPDATE jobs SET status = 'queued', error = NULL, progress = 0 WHERE id = ?",
            (job_id,),
        )


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_job(job_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM summaries WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


# --- LLM 生成結果(要約・議事録) ---

def save_summary(job_id: str, kind: str, content: str, model: str) -> dict[str, Any]:
    """生成結果を保存する。同じジョブ×種類は上書き(再生成)。"""
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO summaries (job_id, kind, content, model, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, content, model, now),
        )
    return {"kind": kind, "content": content, "model": model, "created_at": now}


def get_summaries(job_id: str) -> dict[str, dict[str, Any]]:
    """ジョブの生成結果を kind をキーにした辞書で返す。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT kind, content, model, created_at FROM summaries WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        return {r["kind"]: dict(r) for r in rows}


# --- 用語リスト・コンテキストのプリセット ---

def list_presets() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM presets ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def save_preset(name: str, vocabulary: str, context: str) -> dict[str, Any]:
    """同名のプリセットがあれば上書き、なければ新規作成する。"""
    now = time.time()
    with _conn() as conn:
        row = conn.execute("SELECT id FROM presets WHERE name = ?", (name,)).fetchone()
        preset_id = row["id"] if row else uuid.uuid4().hex
        conn.execute(
            "INSERT OR REPLACE INTO presets (id, name, vocabulary, context, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (preset_id, name, vocabulary, context, now),
        )
    return {
        "id": preset_id,
        "name": name,
        "vocabulary": vocabulary,
        "context": context,
        "updated_at": now,
    }


def delete_preset(preset_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        return cur.rowcount > 0

"""アプリ全体の設定。すべて環境変数で上書きできる。"""

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# 文字起こしモデル
MODEL_NAME = _env("WHISPER_MODEL", "small")
DEVICE = _env("WHISPER_DEVICE", "auto")          # auto / cpu / cuda
COMPUTE_TYPE = _env("WHISPER_COMPUTE_TYPE", "auto")  # auto / int8 / float16 ...
DEFAULT_LANGUAGE = _env("WHISPER_LANGUAGE", "") or None  # 空なら自動判定

# データ永続化
DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "app.db"

# 同時実行制御
JOB_WORKERS = int(_env("JOB_WORKERS", "1"))
MAX_REALTIME_SESSIONS = int(_env("MAX_REALTIME_SESSIONS", "2"))

# アップロード上限(バイト)
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))


def ensure_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

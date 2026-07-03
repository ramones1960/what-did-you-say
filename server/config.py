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

# 話者分離
DIARIZATION_ENABLED = _env("DIARIZATION", "1") not in ("0", "false", "off")
SPEAKER_MODEL_PATH = _env("SPEAKER_MODEL_PATH", "")  # ローカルの .onnx を使う場合
SPEAKER_MODEL_REPO = _env("SPEAKER_MODEL_REPO", "csukuangfj/speaker-embedding-models")
SPEAKER_MODEL_FILE = _env(
    "SPEAKER_MODEL_FILE",
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
)
# 同一話者とみなすコサイン類似度のしきい値(下げると話者がまとまりやすい)
SPEAKER_THRESHOLD = float(_env("SPEAKER_THRESHOLD", "0.4"))

# 同時実行制御
JOB_WORKERS = int(_env("JOB_WORKERS", "1"))
MAX_REALTIME_SESSIONS = int(_env("MAX_REALTIME_SESSIONS", "2"))

# アップロード上限(バイト)
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))


def ensure_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

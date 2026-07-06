"""pytest 共通設定。

server.config は import 時に環境変数を読むため、server パッケージを
import する前に(このファイルの先頭で)テスト用の環境変数を設定する。
- DATA_DIR はセッションごとの一時ディレクトリ(実データを壊さない)
- 話者分離・LLM は無効化し、モデルのダウンロードを発生させない
"""

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wdys-test-")
os.environ["DIARIZATION"] = "0"
os.environ["LLM_API_URL"] = ""
os.environ["JOB_WORKERS"] = "1"

import pytest

from server import config, db


@pytest.fixture()
def fresh_db():
    """テストごとに空の DB を用意する。"""
    config.ensure_dirs()
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.init_db()
    yield
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()

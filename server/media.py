"""ffmpeg / ffprobe による音声・動画ファイルの変換。"""

import json
import subprocess
from pathlib import Path


class MediaError(Exception):
    pass


def to_wav16k(src: Path, dst: Path) -> None:
    """任意の音声・動画ファイルを 16kHz mono WAV に変換する。"""
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MediaError(f"音声の抽出に失敗しました: {proc.stderr.strip()[:500]}")


def probe_duration(path: Path) -> float | None:
    """メディアファイルの長さ(秒)を返す。取得できなければ None。"""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None

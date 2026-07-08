"""ffmpeg / ffprobe による音声・動画ファイルの変換。"""

import json
import subprocess
from pathlib import Path


class MediaError(Exception):
    pass


def _explain_ffmpeg_error(stderr: str) -> str:
    """ffmpeg の stderr を利用者向けの説明文に変換する。

    よくある失敗は原因と対処を添えた日本語にし、未知のエラーは原文
    (先頭 500 字)をそのまま返して調査できるようにする。
    """
    detail = stderr.strip()
    low = detail.lower()
    if "moov atom not found" in low:
        # MP4/MOV のメタデータ(moov atom)はファイル末尾に書かれ、録画が
        # 正常終了して初めて確定する。OBS の MP4 録画中にクラッシュ・強制終了・
        # 電源断があると moov が書かれず、この状態のファイルはどの再生ソフトでも
        # 開けない(壊れている)。ffmpeg も EOF まで読んで moov を見つけられず失敗する。
        return (
            "動画ファイルが壊れています(メタデータ moov atom が見つかりません)。"
            "録画が途中で中断されると発生し、OBS で MP4 録画中にクラッシュ・"
            "強制終了・電源断があった場合によく起こります。OBS の「録画のリマックス」"
            "で修復を試すか、録画フォーマットを MKV に変更して録り直してください。"
        )
    return f"音声の抽出に失敗しました: {detail[:500]}"


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
        raise MediaError(_explain_ffmpeg_error(proc.stderr))


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

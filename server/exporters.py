"""文字起こし結果のエクスポート(TXT / SRT / VTT)とセグメント結合。

speaker_names は {"1": "山田", ...} 形式のマップ。話者番号があり
氏名が未設定の場合は「話者N」の仮名のまま出力する。

セグメント結合(発言の区切り):
認識は細かい粒度で DB に保存し、TXT 出力時に granularity 指定で結合する。
SRT / VTT は字幕用途のため常に細かい粒度のまま。
パラメータの正典は shared/merge_params.json(フロント側 web/src/lib.ts も
同じファイルを読むため、値の変更はそのファイルだけでよい)。
結合ルールのロジック自体は lib.ts の mergeSegments と揃えること。
"""

import json as _json
import re
from pathlib import Path
from typing import Any

Names = dict[str, str] | None

# 結合パラメータ: (結合する無音間隔[秒], 結合後の最大長[秒], 最大文字数)
# short は結合しない(認識されたままの粒度)。正典は shared/merge_params.json
_MERGE_PARAMS_FILE = Path(__file__).resolve().parent.parent / "shared" / "merge_params.json"
MERGE_PARAMS: dict[str, tuple[float, float, int] | None] = {
    name: (p["gap"], p["max_duration"], p["max_chars"]) if p else None
    for name, p in _json.loads(_MERGE_PARAMS_FILE.read_text(encoding="utf-8")).items()
}


def _join_text(a: str, b: str) -> str:
    """テキストを連結する。欧文どうし(前が ASCII で終わり、次が英数字で
    始まる)のときだけ空白を挟む。日本語どうしは空白なしで繋がる。"""
    if a and ord(a[-1]) < 128 and a[-1] != " " and re.match(r"[A-Za-z0-9]", b):
        return f"{a} {b}"
    return a + b


def merge_segments(
    segments: list[dict[str, Any]], granularity: str
) -> list[dict[str, Any]]:
    """連続セグメントを「同一話者・間隔・上限」の条件で結合する。

    時刻タグは結合ブロック先頭の時刻になる。granularity が未知の値の
    場合は結合しない。
    """
    params = MERGE_PARAMS.get(granularity)
    if params is None or not segments:
        return segments
    gap, max_dur, max_chars = params

    merged: list[dict[str, Any]] = []
    for seg in segments:
        last = merged[-1] if merged else None
        if (
            last is not None
            and seg.get("speaker") == last.get("speaker")
            and seg["start"] - last["end"] < gap
            and seg["end"] - last["start"] <= max_dur
            and len(last["text"]) + len(seg["text"]) <= max_chars
        ):
            last["end"] = seg["end"]
            last["text"] = _join_text(last["text"], seg["text"])
        else:
            merged.append(dict(seg))
    return merged


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def speaker_label(speaker: int | None, names: Names) -> str | None:
    if speaker is None:
        return None
    if names and str(speaker) in names and names[str(speaker)].strip():
        return names[str(speaker)].strip()
    return f"話者{speaker}"


def _line_text(seg: dict[str, Any], names: Names) -> str:
    label = speaker_label(seg.get("speaker"), names)
    return f"{label}: {seg['text']}" if label else seg["text"]


def to_txt(segments: list[dict[str, Any]], names: Names = None) -> str:
    return (
        "\n".join(f"[{_hms(s['start'])}] {_line_text(s, names)}" for s in segments)
        + "\n"
    )


def to_srt(segments: list[dict[str, Any]], names: Names = None) -> str:
    blocks = [
        f"{i + 1}\n{_srt_time(s['start'])} --> {_srt_time(s['end'])}\n{_line_text(s, names)}\n"
        for i, s in enumerate(segments)
    ]
    return "\n".join(blocks)


def to_vtt(segments: list[dict[str, Any]], names: Names = None) -> str:
    blocks = [
        f"{_vtt_time(s['start'])} --> {_vtt_time(s['end'])}\n{_line_text(s, names)}\n"
        for s in segments
    ]
    return "WEBVTT\n\n" + "\n".join(blocks)


FORMATS = {
    "txt": (to_txt, "text/plain; charset=utf-8", "txt"),
    "srt": (to_srt, "application/x-subrip; charset=utf-8", "srt"),
    "vtt": (to_vtt, "text/vtt; charset=utf-8", "vtt"),
}

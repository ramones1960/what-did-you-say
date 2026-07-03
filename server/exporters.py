"""文字起こし結果のエクスポート(TXT / SRT / VTT)。"""

from typing import Any


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def to_txt(segments: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{_hms(s['start'])}] {s['text']}" for s in segments) + "\n"


def to_srt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{i + 1}\n{_srt_time(s['start'])} --> {_srt_time(s['end'])}\n{s['text']}\n"
        for i, s in enumerate(segments)
    ]
    return "\n".join(blocks)


def to_vtt(segments: list[dict[str, Any]]) -> str:
    blocks = [
        f"{_vtt_time(s['start'])} --> {_vtt_time(s['end'])}\n{s['text']}\n"
        for s in segments
    ]
    return "WEBVTT\n\n" + "\n".join(blocks)


FORMATS = {
    "txt": (to_txt, "text/plain; charset=utf-8", "txt"),
    "srt": (to_srt, "application/x-subrip; charset=utf-8", "srt"),
    "vtt": (to_vtt, "text/vtt; charset=utf-8", "vtt"),
}

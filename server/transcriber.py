"""faster-whisper モデルのラッパー。

モデルは重いのでプロセス内シングルトンとして遅延ロードする。
transcribe_* はブロッキング処理なので、呼び出し側で
asyncio.to_thread などを使ってイベントループの外で実行すること。
"""

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

from . import config

_model: WhisperModel | None = None
_model_lock = threading.Lock()

# GPU/CPU を問わずモデルの同時実行は 1 本に絞る(v1)。
# 並列度を上げたい場合はワーカープロセスを分ける方が安全。
inference_lock = threading.Lock()


def get_model() -> WhisperModel:
    global _model
    with _model_lock:
        if _model is None:
            _model = WhisperModel(
                config.MODEL_NAME,
                device=config.DEVICE,
                compute_type=config.COMPUTE_TYPE,
            )
        return _model


class Segment:
    __slots__ = ("start", "end", "text")

    def __init__(self, start: float, end: float, text: str):
        self.start = start
        self.end = end
        self.text = text


def normalize_vocabulary(text: str | None) -> str | None:
    """改行・カンマ区切りの用語リストを hotwords 用の1行文字列に正規化する。"""
    if not text:
        return None
    terms = [t.strip() for chunk in text.splitlines() for t in chunk.split(",")]
    terms = [t for t in terms if t]
    return ", ".join(dict.fromkeys(terms)) or None


def transcribe_file(
    path: Path,
    language: str | None,
    on_segment: Callable[[Segment], None],
    on_info: Callable[[str, float], None] | None = None,
    vocabulary: str | None = None,
    context: str | None = None,
) -> None:
    """ファイルを文字起こしし、セグメントごとに on_segment を呼ぶ。

    vocabulary(用語リスト)は hotwords として全ウィンドウの認識を誘導し、
    context は initial_prompt として冒頭の文脈・文体を与える。
    どちらもバイアスであり、確実な置換ではない。
    """
    model = get_model()
    with inference_lock:
        segments, info = model.transcribe(
            str(path),
            language=language or config.DEFAULT_LANGUAGE,
            vad_filter=True,
            beam_size=5,
            hotwords=normalize_vocabulary(vocabulary),
            initial_prompt=context or None,
        )
        if on_info is not None:
            on_info(info.language, info.duration)
        for seg in segments:
            text = seg.text.strip()
            if text:
                on_segment(Segment(seg.start, seg.end, text))


def transcribe_pcm(
    audio: np.ndarray,
    language: str | None,
    vocabulary: str | None = None,
    context: str | None = None,
) -> Iterator[Segment]:
    """float32 mono 16kHz の numpy 配列を文字起こしする(リアルタイム用)。"""
    model = get_model()
    with inference_lock:
        segments, _info = model.transcribe(
            audio,
            language=language or config.DEFAULT_LANGUAGE,
            beam_size=5,
            condition_on_previous_text=False,
            hotwords=normalize_vocabulary(vocabulary),
            initial_prompt=context or None,
        )
        for seg in segments:
            text = seg.text.strip()
            if text:
                yield Segment(seg.start, seg.end, text)

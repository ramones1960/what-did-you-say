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


def transcribe_file(
    path: Path,
    language: str | None,
    on_segment: Callable[[Segment], None],
    on_info: Callable[[str, float], None] | None = None,
) -> None:
    """ファイルを文字起こしし、セグメントごとに on_segment を呼ぶ。"""
    model = get_model()
    with inference_lock:
        segments, info = model.transcribe(
            str(path),
            language=language or config.DEFAULT_LANGUAGE,
            vad_filter=True,
            beam_size=5,
        )
        if on_info is not None:
            on_info(info.language, info.duration)
        for seg in segments:
            text = seg.text.strip()
            if text:
                on_segment(Segment(seg.start, seg.end, text))


def transcribe_pcm(audio: np.ndarray, language: str | None) -> Iterator[Segment]:
    """float32 mono 16kHz の numpy 配列を文字起こしする(リアルタイム用)。"""
    model = get_model()
    with inference_lock:
        segments, _info = model.transcribe(
            audio,
            language=language or config.DEFAULT_LANGUAGE,
            beam_size=5,
            condition_on_previous_text=False,
        )
        for seg in segments:
            text = seg.text.strip()
            if text:
                yield Segment(seg.start, seg.end, text)

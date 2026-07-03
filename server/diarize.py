"""話者分離(スピーカーダイアライゼーション)。

sherpa-onnx の話者埋め込みモデルでセグメントごとの声紋ベクトルを取り、
オンラインクラスタリング(セントロイドとのコサイン類似度)で
「話者1」「話者2」… の仮ラベルを割り当てる。氏名はあとから
speaker_names マッピングで一括適用する(exporters / フロント側)。

モデルは初回利用時に Hugging Face から取得し、HF キャッシュ
(Docker では model_cache ボリューム)に保存される。取得できない
環境では話者タグなしで従来どおり動作する。
"""

import logging
import threading
from pathlib import Path

import numpy as np

from . import config

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 話者の埋め込みを取るのに最低限必要な長さ(これ未満は直前の話者を継承)
MIN_EMBED_SECONDS = 0.5

_extractor = None
_load_failed = False
_lock = threading.Lock()


def _model_path() -> str:
    local = config.SPEAKER_MODEL_PATH
    if local and Path(local).exists():
        return str(local)
    from huggingface_hub import hf_hub_download

    return hf_hub_download(
        repo_id=config.SPEAKER_MODEL_REPO, filename=config.SPEAKER_MODEL_FILE
    )


def _get_extractor():
    global _extractor, _load_failed
    with _lock:
        if _extractor is None and not _load_failed and config.DIARIZATION_ENABLED:
            try:
                import sherpa_onnx

                cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=_model_path(), num_threads=2
                )
                _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
                logger.info("speaker embedding model loaded")
            except Exception:
                _load_failed = True
                logger.exception(
                    "話者分離モデルを読み込めませんでした。話者タグなしで続行します"
                )
        return _extractor


def available() -> bool:
    return _get_extractor() is not None


class SpeakerTracker:
    """1つの文字起こし(ジョブ or リアルタイムセッション)内の話者を追跡する。"""

    def __init__(self) -> None:
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._last: int | None = None

    def assign(self, audio: np.ndarray) -> int | None:
        """float32 mono 16kHz のセグメント音声に話者番号(1始まり)を割り当てる。"""
        extractor = _get_extractor()
        if extractor is None:
            return None
        if len(audio) < int(MIN_EMBED_SECONDS * SAMPLE_RATE):
            return self._last  # 短すぎて判定できない → 直前の話者を継承

        with _lock:
            stream = extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            stream.input_finished()
            vec = np.array(extractor.compute(stream), dtype=np.float32)

        norm = np.linalg.norm(vec)
        if norm == 0:
            return self._last
        v = vec / norm

        if self._centroids:
            sims = [
                float(np.dot(v, c) / np.linalg.norm(c)) for c in self._centroids
            ]
            best = int(np.argmax(sims))
            if sims[best] >= config.SPEAKER_THRESHOLD:
                n = self._counts[best]
                self._centroids[best] = (self._centroids[best] * n + v) / (n + 1)
                self._counts[best] += 1
                self._last = best + 1
                return self._last

        self._centroids.append(v.copy())
        self._counts.append(1)
        self._last = len(self._centroids)
        return self._last

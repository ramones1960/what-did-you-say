"""話者分離(スピーカーダイアライゼーション)。

2つの方式を持つ:

- **逐次(オンライン)割り当て** … SpeakerTracker。セグメントごとに sherpa-onnx の
  話者埋め込み(声紋ベクトル)を取り、既存話者セントロイドとのコサイン類似度で
  「話者1」「話者2」… の仮ラベルを割り当てる。リアルタイム文字起こしと、
  ファイル文字起こしの処理中の暫定表示に使う
- **一括(オフライン)話者分離** … diarize_offline / map_speakers。音声全体を
  sherpa-onnx の OfflineSpeakerDiarization(pyannote segmentation-3.0 の ONNX 版 +
  話者埋め込み)で解析し、話者区間とセグメントの時間重なりでラベルを割り当て直す。
  処理順に依存せず話者交代の検出も行うため逐次より精度が高い。ファイル文字起こしの
  完了時に jobs.py が呼び、逐次割り当ての結果を置き換える

氏名はあとから speaker_names マッピングで一括適用する(exporters / フロント側)。
モデルは初回利用時に Hugging Face から取得し、HF キャッシュ
(Docker では model_cache ボリューム)に保存される。取得できない
環境では話者タグなしで従来どおり動作する。
"""

import logging
import threading
from collections.abc import Callable
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


# --- 一括(オフライン)話者分離 ---

_diarizer = None
_diarizer_failed = False
# 一括処理は数十秒〜数分かかるため、リアルタイムの埋め込み抽出(_lock)とは
# 別のロックで直列化する(同時ジョブどうしの競合防止)
_offline_lock = threading.Lock()


def _segmentation_model_path() -> str:
    local = config.SEGMENTATION_MODEL_PATH
    if local and Path(local).exists():
        return str(local)
    from huggingface_hub import hf_hub_download

    return hf_hub_download(
        repo_id=config.SEGMENTATION_MODEL_REPO,
        filename=config.SEGMENTATION_MODEL_FILE,
    )


def _diarizer_config(num_clusters: int | None):
    """一括話者分離の設定を組み立てる。

    num_clusters を指定するとクラスタ数を固定し(人数既知の会議で頑健)、
    未指定なら DIARIZATION_CLUSTER_THRESHOLD で自動推定する。
    """
    import sherpa_onnx

    return sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=_segmentation_model_path()
            ),
            num_threads=2,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=_model_path(), num_threads=2
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_clusters or -1,
            threshold=config.DIARIZATION_CLUSTER_THRESHOLD,
        ),
    )


def _get_diarizer():
    global _diarizer, _diarizer_failed
    with _offline_lock:
        if _diarizer is None and not _diarizer_failed and config.DIARIZATION_ENABLED:
            try:
                import sherpa_onnx

                _diarizer = sherpa_onnx.OfflineSpeakerDiarization(
                    _diarizer_config(None)
                )
                logger.info("offline speaker diarization model loaded")
            except Exception:
                _diarizer_failed = True
                logger.exception(
                    "一括話者分離モデルを読み込めませんでした。逐次割り当ての結果のまま続行します"
                )
        return _diarizer


def offline_available() -> bool:
    return _get_diarizer() is not None


def diarize_offline(
    audio: np.ndarray,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> list[tuple[float, float, int]]:
    """音声全体(float32 mono 16kHz)に一括話者分離をかけて話者区間を返す。

    話者の人数は最小〜最大の幅で指定できる(参加者全員が発話するとは
    限らないため)。同数ならクラスタ数を固定して1回で処理し、幅がある
    場合はまず自動推定し、結果が範囲を外れたときだけ近い方の境界値に
    クラスタ数を固定してやり直す(その場合のみ処理時間が約2倍になる)。

    戻り値は (start, end, 話者ID) のリスト(開始時刻順)。話者 ID は 0 始まりの
    生のクラスタ番号で、「話者N」への振り直しは map_speakers が行う。
    should_abort が True を返すと途中で打ち切る(不完全な結果になるため、
    呼び出し側は中断時の戻り値を使わないこと)。
    """
    diarizer = _get_diarizer()
    if diarizer is None:
        return []

    def callback(_done: int, _total: int) -> int:
        return 1 if should_abort is not None and should_abort() else 0

    def run(num_clusters: int | None) -> list[tuple[float, float, int]]:
        # クラスタリング設定(人数指定)だけを差し替える。モデルは再ロードされない
        diarizer.set_config(_diarizer_config(num_clusters))
        result = diarizer.process(audio, callback=callback)
        return [(s.start, s.end, s.speaker) for s in result.sort_by_start_time()]

    fixed = min_speakers if min_speakers is not None and min_speakers == max_speakers else None
    with _offline_lock:
        turns = run(fixed)
        if fixed is None and turns:
            found = len({spk for _, _, spk in turns})
            if max_speakers is not None and found > max_speakers:
                turns = run(max_speakers)
            elif min_speakers is not None and found < min_speakers:
                turns = run(min_speakers)
    return turns


def map_speakers(
    segments: list[dict], turns: list[tuple[float, float, int]]
) -> dict[int, int]:
    """一括話者分離の話者区間を文字起こしセグメントへ割り当てる。

    各セグメント(idx / start / end を持つ辞書)に対し、時間の重なりが最大の
    話者区間を採用する。重なる区間が無いセグメント(無音際など)は最も近い
    区間に寄せる。話者番号は登場順に 1 始まりで振り直し、「話者N」の
    セッション内連番の慣例に合わせる。戻り値は {セグメント idx: 話者番号}。
    turns が空(発話区間なし・処理不能)なら空辞書を返し、呼び出し側は
    既存の割り当てを維持する。
    """
    if not turns:
        return {}
    renumber: dict[int, int] = {}  # 生のクラスタ番号 → 登場順の話者番号
    mapping: dict[int, int] = {}
    for seg in segments:
        best: int | None = None
        best_overlap = 0.0
        for start, end, spk in turns:
            overlap = min(seg["end"], end) - max(seg["start"], start)
            if overlap > best_overlap:
                best, best_overlap = spk, overlap
        if best is None:
            best_dist: float | None = None
            for start, end, spk in turns:
                dist = max(start - seg["end"], seg["start"] - end, 0.0)
                if best_dist is None or dist < best_dist:
                    best, best_dist = spk, dist
        if best not in renumber:
            renumber[best] = len(renumber) + 1
        mapping[seg["idx"]] = renumber[best]
    return mapping

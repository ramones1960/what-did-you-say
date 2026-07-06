"""ファイル文字起こしのジョブキューとワーカー。

処理の流れ:
  1. main.py の POST /api/jobs がアップロードを保存し enqueue() する
  2. ワーカー(asyncio タスク)がキューから取り出し _process_job() を実行
  3. ffmpeg で 16kHz mono WAV に変換 → faster-whisper で文字起こし
  4. セグメントは確定するたびに DB へ書き込まれるため、クライアントは
     GET /api/jobs/{id} のポーリングで途中経過を取得できる

ジョブの状態遷移: queued → processing → done / error
(進捗はセグメント末尾時刻 / 音声全長で 0.0〜1.0)

v1 はインプロセスの asyncio.Queue + ワーカータスク。プロセス再起動で
キュー内容は失われるため、起動時に requeue_stale_jobs() が DB 上に
queued / processing のまま残ったジョブを回収して再投入する(元ファイルが
残っていれば最初からやり直し、消えていれば error にする)。社内公開で
スケールさせる場合は、このモジュールを Redis キュー + 別プロセスワーカーに
差し替える(インターフェース: enqueue / start_workers / stop_workers)。
"""

import asyncio
import logging
import tempfile
import wave
from pathlib import Path

import numpy as np

from . import config, db, diarize, media, transcriber

logger = logging.getLogger(__name__)

_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []


def upload_path(job_id: str) -> Path:
    """アップロードされた元ファイルの保存先(ジョブ ID がファイル名)。"""
    return config.UPLOAD_DIR / job_id


async def enqueue(job_id: str) -> None:
    """ジョブをキューに積む。DB 上のジョブは作成済みであること。"""
    await _queue.put(job_id)


def requeue_stale_jobs() -> list[str]:
    """再起動で宙に浮いたジョブ(queued / processing)を回収する。

    キューはインプロセスのため、再起動すると DB 上のジョブとキューの中身が
    食い違う。元ファイルが残っていれば途中結果を破棄して queued に戻し、
    再投入対象のジョブ ID リストを返す。元ファイルが無い(異常系)ジョブは
    再開できないので error にする。アプリ起動時に start_workers() から呼ばれる。
    """
    requeued: list[str] = []
    for job in db.list_jobs_by_status(("queued", "processing")):
        job_id = job["id"]
        if upload_path(job_id).exists():
            db.reset_job(job_id)
            requeued.append(job_id)
            logger.info("job %s を再投入します(再起動により中断)", job_id)
        else:
            db.set_status(
                job_id, "error", "サーバー再起動により中断されました。再度アップロードしてください"
            )
            logger.warning("job %s は元ファイルが無いため再開できません", job_id)
    return requeued


def start_workers() -> None:
    """JOB_WORKERS 個のワーカータスクを起動する(アプリ起動時に呼ばれる)。

    起動前に、前回の実行から DB に残ったジョブをキューへ再投入する。
    """
    for job_id in requeue_stale_jobs():
        _queue.put_nowait(job_id)
    for i in range(config.JOB_WORKERS):
        _workers.append(asyncio.create_task(_worker_loop(i)))


async def stop_workers() -> None:
    """全ワーカーを停止する(アプリ終了時に呼ばれる)。"""
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()


async def _worker_loop(worker_id: int) -> None:
    """キューからジョブを取り出して順に処理するループ。

    文字起こしはブロッキング処理なので to_thread でイベントループの
    外に逃がす。例外はジョブを error にして握りつぶし、ループは続行する。
    """
    logger.info("job worker %d started", worker_id)
    while True:
        job_id = await _queue.get()
        try:
            await asyncio.to_thread(_process_job, job_id)
        except Exception:
            logger.exception("job %s failed", job_id)
            db.set_status(job_id, "error", "内部エラーが発生しました")
        finally:
            _queue.task_done()


def _process_job(job_id: str) -> None:
    """1ジョブを最後まで処理する(ワーカースレッド内で実行される)。

    変換 → 話者分離の準備 → 文字起こし(セグメントごとに DB へ書き込み)
    → 完了処理。元ファイルは完了時に削除し、結果だけを DB に残す。
    """
    job = db.get_job(job_id)
    if job is None:
        return
    db.set_status(job_id, "processing")
    src = upload_path(job_id)

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        try:
            media.to_wav16k(src, wav)
        except media.MediaError as e:
            db.set_status(job_id, "error", str(e))
            return

        duration = media.probe_duration(wav)
        db.set_duration(job_id, duration)

        # 話者分離用に音声全体を読み込む(16kHz mono 16bit WAV)
        audio: np.ndarray | None = None
        tracker: diarize.SpeakerTracker | None = None
        if diarize.available():
            with wave.open(str(wav), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            tracker = diarize.SpeakerTracker()

        counter = {"idx": 0}

        def on_info(language: str, _dur: float) -> None:
            db.set_language(job_id, language)

        def on_segment(seg: transcriber.Segment) -> None:
            speaker = None
            if tracker is not None and audio is not None:
                clip = audio[int(seg.start * 16000):int(seg.end * 16000)]
                speaker = tracker.assign(clip)
            db.add_segment(
                job_id, counter["idx"], seg.start, seg.end, seg.text, speaker
            )
            counter["idx"] += 1
            if duration:
                db.set_progress(job_id, seg.end / duration)

        try:
            transcriber.transcribe_file(
                wav,
                job["language"],
                on_segment,
                on_info,
                vocabulary=job.get("vocabulary"),
                context=job.get("context"),
            )
        except Exception as e:
            logger.exception("transcription failed for job %s", job_id)
            db.set_status(job_id, "error", f"文字起こしに失敗しました: {e}")
            return

    db.set_progress(job_id, 1.0)
    db.set_status(job_id, "done")
    # 元ファイルは文字起こし完了後に削除(結果は DB に残る)
    src.unlink(missing_ok=True)

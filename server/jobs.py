"""ファイル文字起こしのジョブキューとワーカー。

v1 はインプロセスの asyncio.Queue + ワーカータスク。
社内公開でスケールさせる場合は、このモジュールを
Redis キュー + 別プロセスワーカーに差し替える。
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from . import config, db, media, transcriber

logger = logging.getLogger(__name__)

_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []


def upload_path(job_id: str) -> Path:
    return config.UPLOAD_DIR / job_id


async def enqueue(job_id: str) -> None:
    await _queue.put(job_id)


def start_workers() -> None:
    for i in range(config.JOB_WORKERS):
        _workers.append(asyncio.create_task(_worker_loop(i)))


async def stop_workers() -> None:
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()


async def _worker_loop(worker_id: int) -> None:
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

        counter = {"idx": 0}

        def on_info(language: str, _dur: float) -> None:
            db.set_language(job_id, language)

        def on_segment(seg: transcriber.Segment) -> None:
            db.add_segment(job_id, counter["idx"], seg.start, seg.end, seg.text)
            counter["idx"] += 1
            if duration:
                db.set_progress(job_id, seg.end / duration)

        try:
            transcriber.transcribe_file(wav, job["language"], on_segment, on_info)
        except Exception as e:
            logger.exception("transcription failed for job %s", job_id)
            db.set_status(job_id, "error", f"文字起こしに失敗しました: {e}")
            return

    db.set_progress(job_id, 1.0)
    db.set_status(job_id, "done")
    # 元ファイルは文字起こし完了後に削除(結果は DB に残る)
    src.unlink(missing_ok=True)

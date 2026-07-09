"""ファイル文字起こしのジョブキューとワーカー。

処理の流れ:
  1. main.py の POST /api/jobs がアップロードを保存し enqueue() する
  2. ワーカー(asyncio タスク)がキューから取り出し _process_job() を実行
  3. ffmpeg で 16kHz mono WAV に変換 → faster-whisper で文字起こし
  4. セグメントは確定するたびに DB へ書き込まれるため、クライアントは
     GET /api/jobs/{id} のポーリングで途中経過を取得できる
     (話者ラベルは逐次割り当ての暫定値)
  5. 完了前に音声全体を通した一括話者分離(diarize.diarize_offline)で
     話者ラベルを置き換えて確定する(失敗時は暫定値のまま完了)

ジョブの状態遷移: queued → processing → done / error / canceled
(進捗はセグメント末尾時刻 / 音声全長で 0.0〜1.0)

中断(キャンセル): main.py の POST /api/jobs/{id}/cancel が request_cancel() を
呼ぶと、その ID が _cancel_requested に入る。ワーカーは処理の要所(変換後・
セグメント確定ごと)でこのフラグを見て JobCanceled を送出し、状態を canceled に
して途中結果はそのまま残す(元ファイルのみ削除)。Whisper はセグメント境界でしか
止められないため、中断は「次のセグメントが確定した時」に反映される。通常の会話音声
(無音でセグメントが細かく区切れる)なら数秒だが、無音が少なく1セグメントが長い
音声では、そのセグメントの推論が終わるまで反映が遅れる。

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

# 中断が要求されたジョブ ID(プロセス内メモリ)。ワーカーが要所で参照する。
_cancel_requested: set[str] = set()


class JobCanceled(Exception):
    """ユーザーの中断要求によって処理を打ち切るための内部例外。"""


def upload_path(job_id: str) -> Path:
    """アップロードされた元ファイルの保存先(ジョブ ID がファイル名)。"""
    return config.UPLOAD_DIR / job_id


async def enqueue(job_id: str) -> None:
    """ジョブをキューに積む。DB 上のジョブは作成済みであること。"""
    await _queue.put(job_id)


def request_cancel(job_id: str) -> None:
    """ジョブの中断を要求する(main.py の cancel エンドポイントから呼ばれる)。

    実際の停止はワーカーが次にフラグを確認したとき(セグメント確定時など)に
    行われる。まだキュー内で処理が始まっていないジョブも、ワーカーが取り出した
    時点でこのフラグを見て即座に canceled になる。
    """
    _cancel_requested.add(job_id)


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


def _finalize_canceled(job_id: str) -> None:
    """中断されたジョブを canceled 状態にし、元ファイルを片付ける。

    途中まで確定したセグメントはそのまま残す(非破壊)。ユーザーはそこまでの
    結果を確認・エクスポートでき、不要なら通常どおり削除できる。
    """
    db.set_status(job_id, "canceled")
    upload_path(job_id).unlink(missing_ok=True)
    logger.info("job %s を中断しました", job_id)


def _process_job(job_id: str) -> None:
    """1ジョブを最後まで処理する(ワーカースレッド内で実行される)。

    変換 → 話者分離の準備 → 文字起こし(セグメントごとに DB へ書き込み)
    → 完了処理。元ファイルは完了時に削除し、結果だけを DB に残す。
    中断が要求されていれば要所で JobCanceled を送出して canceled にする。
    """
    job = db.get_job(job_id)
    if job is None:
        return
    # キュー待ちの間に中断されていたら、処理を始めずに片付ける
    if job_id in _cancel_requested:
        _finalize_canceled(job_id)
        return
    db.set_status(job_id, "processing")
    src = upload_path(job_id)

    def check_canceled() -> None:
        if job_id in _cancel_requested:
            raise JobCanceled()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            try:
                media.to_wav16k(src, wav)
            except media.MediaError as e:
                db.set_status(job_id, "error", str(e))
                return
            check_canceled()

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
                # セグメント境界ごとに中断要求を確認する(唯一止められる箇所)
                check_canceled()
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
            except JobCanceled:
                raise
            except Exception as e:
                logger.exception("transcription failed for job %s", job_id)
                db.set_status(
                    job_id,
                    "error",
                    f"文字起こしに失敗しました: {transcriber.explain_inference_error(e)}",
                )
                return

        check_canceled()

        # 完了前に、音声全体を通した一括話者分離で逐次(オンライン)割り当てを
        # 置き換える。処理順に依存しないため精度が高い(diarize.py 参照)。
        # 失敗しても致命的ではないので、その場合は逐次割り当ての結果のまま完了する
        if audio is not None and diarize.offline_available():
            try:
                turns = diarize.diarize_offline(
                    audio,
                    num_speakers=job.get("num_speakers"),
                    should_abort=lambda: job_id in _cancel_requested,
                )
                # 中断で打ち切られた場合は不完全な結果を適用せず canceled へ
                check_canceled()
                speakers = diarize.map_speakers(db.get_segments(job_id), turns)
                if speakers:
                    db.update_segment_speakers(job_id, speakers)
            except JobCanceled:
                raise
            except Exception:
                logger.exception(
                    "job %s の一括話者分離に失敗しました(逐次割り当ての結果のまま完了します)",
                    job_id,
                )

        db.set_progress(job_id, 1.0)
        db.set_status(job_id, "done")
        # 元ファイルは文字起こし完了後に削除(結果は DB に残る)
        src.unlink(missing_ok=True)
    except JobCanceled:
        _finalize_canceled(job_id)
    finally:
        _cancel_requested.discard(job_id)

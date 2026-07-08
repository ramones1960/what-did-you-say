"""jobs.py の再起動時リカバリ(requeue_stale_jobs)と中断処理のテスト。"""

from pathlib import Path

from server import config, db, jobs, media, transcriber


class TestRequeueStaleJobs:
    def test_元ファイルが残っていれば再投入される(self, fresh_db):
        job_id = db.create_job("a.mp3", None)
        db.set_status(job_id, "processing")
        db.add_segment(job_id, 0, 0, 1, "途中結果")
        jobs.upload_path(job_id).write_bytes(b"dummy audio")

        try:
            requeued = jobs.requeue_stale_jobs()
            assert requeued == [job_id]
            job = db.get_job(job_id)
            assert job["status"] == "queued"
            assert db.get_segments(job_id) == []  # 最初からやり直すため途中結果は破棄
        finally:
            jobs.upload_path(job_id).unlink(missing_ok=True)

    def test_元ファイルが無ければエラーになる(self, fresh_db):
        job_id = db.create_job("a.mp3", None)
        db.set_status(job_id, "processing")

        assert jobs.requeue_stale_jobs() == []
        job = db.get_job(job_id)
        assert job["status"] == "error"
        assert "再起動" in job["error"]

    def test_完了済みジョブには触らない(self, fresh_db):
        done = db.create_job("done.mp3", None)
        db.set_status(done, "done")
        error = db.create_job("err.mp3", None)
        db.set_status(error, "error", "失敗")

        assert jobs.requeue_stale_jobs() == []
        assert db.get_job(done)["status"] == "done"
        assert db.get_job(error)["status"] == "error"

    def test_queuedのままのジョブも再投入される(self, fresh_db):
        job_id = db.create_job("q.mp3", None)
        jobs.upload_path(job_id).write_bytes(b"dummy audio")
        try:
            assert jobs.requeue_stale_jobs() == [job_id]
            assert db.get_job(job_id)["status"] == "queued"
        finally:
            jobs.upload_path(job_id).unlink(missing_ok=True)


class TestCancel:
    def test_キュー待ち中に中断済みなら処理せず片付ける(self, fresh_db, monkeypatch):
        # ワーカーが取り出す前に中断が要求されたケース。推論・ffmpeg には触れない。
        called = False

        def fail_to_wav(src, dst):
            nonlocal called
            called = True

        monkeypatch.setattr(media, "to_wav16k", fail_to_wav)
        job_id = db.create_job("a.mp3", None)
        jobs.upload_path(job_id).write_bytes(b"dummy audio")
        jobs.request_cancel(job_id)
        try:
            jobs._process_job(job_id)
        finally:
            jobs._cancel_requested.discard(job_id)
        assert not called  # 変換すら始めない
        assert db.get_job(job_id)["status"] == "canceled"
        assert not jobs.upload_path(job_id).exists()  # 元ファイルは片付けられる

    def test_処理中の中断はセグメント境界で止まり途中結果は残る(
        self, fresh_db, monkeypatch
    ):
        job_id = db.create_job("a.mp3", None)
        jobs.upload_path(job_id).write_bytes(b"dummy audio")

        monkeypatch.setattr(media, "to_wav16k", lambda src, dst: Path(dst).touch())
        monkeypatch.setattr(media, "probe_duration", lambda wav: 10.0)
        monkeypatch.setattr(jobs.diarize, "available", lambda: False)

        # 1つ目のセグメント確定後に中断を要求 → 2つ目の on_segment で打ち切られる
        def fake_transcribe(wav, language, on_segment, on_info, **kwargs):
            on_info("ja", 10.0)
            on_segment(transcriber.Segment(0.0, 1.0, "最初の発話"))
            jobs.request_cancel(job_id)
            on_segment(transcriber.Segment(1.0, 2.0, "止まるはずの発話"))
            on_segment(transcriber.Segment(2.0, 3.0, "届かない発話"))

        monkeypatch.setattr(transcriber, "transcribe_file", fake_transcribe)
        try:
            jobs._process_job(job_id)
        finally:
            jobs._cancel_requested.discard(job_id)

        assert db.get_job(job_id)["status"] == "canceled"
        segments = db.get_segments(job_id)
        assert [s["text"] for s in segments] == ["最初の発話"]  # 確定済みは残る
        assert not jobs.upload_path(job_id).exists()

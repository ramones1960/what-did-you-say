"""jobs.py の再起動時リカバリ(requeue_stale_jobs)のテスト。"""

from server import config, db, jobs


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

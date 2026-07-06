"""db.py の永続化・マイグレーションのテスト。"""

import sqlite3

from server import config, db


class TestInitDb:
    def test_二回呼んでも壊れない(self, fresh_db):
        db.init_db()
        assert db.list_jobs() == []

    def test_旧スキーマからのマイグレーション(self, fresh_db):
        # vocabulary / context / speaker_names / speaker カラムが無い
        # 旧バージョンの DB を作り、init_db() がカラムを追加できることを確認する
        config.DB_PATH.unlink()
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, language TEXT, duration REAL,
                progress REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL
            );
            CREATE TABLE segments (
                job_id TEXT NOT NULL, idx INTEGER NOT NULL, start REAL NOT NULL,
                end REAL NOT NULL, text TEXT NOT NULL, PRIMARY KEY (job_id, idx)
            );
            INSERT INTO jobs (id, filename, status, created_at) VALUES ('old', 'a.mp3', 'done', 0);
            """
        )
        conn.commit()
        conn.close()

        db.init_db()
        job = db.get_job("old")
        assert job is not None
        assert job["vocabulary"] is None  # 追加されたカラムが読める
        assert job["speaker_names"] is None
        db.add_segment("old", 0, 0, 1, "テスト", speaker=1)  # speaker カラムも追加済み
        assert db.get_segments("old")[0]["speaker"] == 1


class TestJobs:
    def test_ジョブのライフサイクル(self, fresh_db):
        job_id = db.create_job("meeting.mp3", "ja", vocabulary="用語", context="文脈")
        job = db.get_job(job_id)
        assert job["status"] == "queued"
        assert job["vocabulary"] == "用語"

        db.set_status(job_id, "processing")
        db.set_duration(job_id, 12.5)
        db.set_language(job_id, "ja")
        db.set_progress(job_id, 0.5)
        db.add_segment(job_id, 0, 0.0, 2.0, "こんにちは", speaker=1)
        db.set_status(job_id, "done")

        job = db.get_job(job_id)
        assert job["status"] == "done"
        assert job["duration"] == 12.5
        assert db.get_segments(job_id) == [
            {"idx": 0, "start": 0.0, "end": 2.0, "text": "こんにちは", "speaker": 1}
        ]

        db.delete_job(job_id)
        assert db.get_job(job_id) is None
        assert db.get_segments(job_id) == []

    def test_progressは0から1に丸められる(self, fresh_db):
        job_id = db.create_job("a.mp3", None)
        db.set_progress(job_id, 1.7)
        assert db.get_job(job_id)["progress"] == 1.0
        db.set_progress(job_id, -0.2)
        assert db.get_job(job_id)["progress"] == 0.0

    def test_セグメントの差分取得(self, fresh_db):
        job_id = db.create_job("a.mp3", None)
        for i in range(5):
            db.add_segment(job_id, i, i, i + 1, f"s{i}")
        diff = db.get_segments(job_id, offset=3)
        assert [s["idx"] for s in diff] == [3, 4]

    def test_状態でのジョブ一覧とリセット(self, fresh_db):
        queued = db.create_job("q.mp3", None)
        processing = db.create_job("p.mp3", None)
        done = db.create_job("d.mp3", None)
        db.set_status(processing, "processing")
        db.set_status(done, "done")
        db.add_segment(processing, 0, 0, 1, "途中結果")
        db.set_progress(processing, 0.4)

        stale = db.list_jobs_by_status(("queued", "processing"))
        assert {j["id"] for j in stale} == {queued, processing}

        db.reset_job(processing)
        job = db.get_job(processing)
        assert job["status"] == "queued"
        assert job["progress"] == 0
        assert db.get_segments(processing) == []  # 途中結果は破棄される


class TestPresets:
    def test_保存と同名上書き(self, fresh_db):
        saved = db.save_preset("定例", "用語A", "文脈A")
        again = db.save_preset("定例", "用語B", "文脈B")
        assert saved["id"] == again["id"]  # 同名は上書き(ID 維持)
        presets = db.list_presets()
        assert len(presets) == 1
        assert presets[0]["vocabulary"] == "用語B"

    def test_削除(self, fresh_db):
        saved = db.save_preset("定例", "", "")
        assert db.delete_preset(saved["id"]) is True
        assert db.delete_preset(saved["id"]) is False
        assert db.list_presets() == []


class TestSummaries:
    def test_保存と再生成上書き(self, fresh_db):
        job_id = db.create_job("a.mp3", None)
        db.save_summary(job_id, "summary", "要約1", "test-model")
        db.save_summary(job_id, "minutes", "議事録1", "test-model")
        db.save_summary(job_id, "summary", "要約2", "test-model")  # 再生成で上書き

        summaries = db.get_summaries(job_id)
        assert set(summaries) == {"summary", "minutes"}
        assert summaries["summary"]["content"] == "要約2"

    def test_ジョブ削除で消える(self, fresh_db):
        job_id = db.create_job("a.mp3", None)
        db.save_summary(job_id, "summary", "要約", "test-model")
        db.delete_job(job_id)
        assert db.get_summaries(job_id) == {}

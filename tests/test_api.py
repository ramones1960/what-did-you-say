"""REST API のバリデーション・エンドポイントのテスト。

モデル推論は行わない: アップロードしたジョブはワーカーに渡さないよう
enqueue をモックし、LLM 生成は llm.generate をモックする。
"""

import pytest
from fastapi.testclient import TestClient

from server import config, db, jobs, llm
from server.main import app


@pytest.fixture()
def client(fresh_db, monkeypatch):
    # テスト中はワーカーにジョブを渡さない(推論・ffmpeg を動かさないため)
    async def noop_enqueue(job_id):
        pass

    monkeypatch.setattr(jobs, "enqueue", noop_enqueue)
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_モデル名とllm状態を返す(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["model"] == config.MODEL_NAME
        assert body["llm"] == {"enabled": False, "model": None}


class TestCreateJob:
    def test_アップロードでジョブが作られる(self, client):
        res = client.post("/api/jobs", files={"file": ("meeting.mp3", b"dummy")})
        assert res.status_code == 200
        job_id = res.json()["id"]
        assert jobs.upload_path(job_id).read_bytes() == b"dummy"
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "queued"
        assert job["filename"] == "meeting.mp3"

    def test_言語コードの検証(self, client):
        res = client.post(
            "/api/jobs?language=INVALID!", files={"file": ("a.mp3", b"x")}
        )
        assert res.status_code == 400

    def test_用語リストの文字数制限(self, client):
        res = client.post(
            "/api/jobs",
            files={"file": ("a.mp3", b"x")},
            data={"vocabulary": "あ" * 1001},
        )
        assert res.status_code == 400

    def test_空ファイルは拒否(self, client):
        res = client.post("/api/jobs", files={"file": ("a.mp3", b"")})
        assert res.status_code == 400
        assert db.list_jobs() == []  # ジョブが残らない


class TestJobDetail:
    def test_存在しないジョブは404(self, client):
        assert client.get("/api/jobs/nonexistent").status_code == 404

    def test_セグメントの差分取得(self, client):
        job_id = db.create_job("a.mp3", None)
        for i in range(3):
            db.add_segment(job_id, i, i, i + 1, f"s{i}")
        body = client.get(f"/api/jobs/{job_id}?segments_from=2").json()
        assert [s["idx"] for s in body["segments"]] == [2]

    def test_処理中のジョブは削除できない(self, client):
        job_id = db.create_job("a.mp3", None)
        db.set_status(job_id, "processing")
        assert client.delete(f"/api/jobs/{job_id}").status_code == 409

    def test_完了ジョブの削除(self, client):
        job_id = db.create_job("a.mp3", None)
        db.set_status(job_id, "done")
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
        assert db.get_job(job_id) is None


class TestSpeakers:
    def test_話者マッピングの保存と正規化(self, client):
        job_id = db.create_job("a.mp3", None)
        res = client.put(
            f"/api/jobs/{job_id}/speakers",
            json={"names": {"1": " 山田 ", "2": ""}},
        )
        assert res.status_code == 200
        assert res.json()["speaker_names"] == {"1": "山田"}  # 空は除外・trim される

    def test_不正なキーは拒否(self, client):
        job_id = db.create_job("a.mp3", None)
        res = client.put(
            f"/api/jobs/{job_id}/speakers", json={"names": {"abc": "山田"}}
        )
        assert res.status_code == 400


class TestExport:
    def _make_done_job(self):
        job_id = db.create_job("会議.mp3", "ja")
        db.add_segment(job_id, 0, 0.0, 2.0, "こんにちは", speaker=1)
        db.add_segment(job_id, 1, 2.2, 4.0, "世界", speaker=1)
        db.set_status(job_id, "done")
        return job_id

    def test_txtは結合される(self, client):
        job_id = self._make_done_job()
        res = client.get(f"/api/jobs/{job_id}/export?format=txt&granularity=standard")
        assert res.status_code == 200
        assert res.text == "[00:00:00] 話者1: こんにちは世界\n"

    def test_txtのshortは結合されない(self, client):
        job_id = self._make_done_job()
        res = client.get(f"/api/jobs/{job_id}/export?format=txt&granularity=short")
        assert res.text.count("\n") == 2

    def test_srtはgranularityの影響を受けない(self, client):
        job_id = self._make_done_job()
        res = client.get(f"/api/jobs/{job_id}/export?format=srt&granularity=long")
        assert "こんにちは" in res.text and "世界" in res.text
        assert res.text.count("-->") == 2

    def test_jsonは生データ(self, client):
        job_id = self._make_done_job()
        body = client.get(f"/api/jobs/{job_id}/export?format=json").json()
        assert len(body["segments"]) == 2

    def test_未対応フォーマットは400(self, client):
        job_id = self._make_done_job()
        assert client.get(f"/api/jobs/{job_id}/export?format=doc").status_code == 400


class TestPresets:
    def test_保存一覧削除(self, client):
        res = client.post(
            "/api/presets", json={"name": "定例", "vocabulary": "用語", "context": ""}
        )
        assert res.status_code == 200
        preset_id = res.json()["id"]
        assert len(client.get("/api/presets").json()) == 1
        assert client.delete(f"/api/presets/{preset_id}").status_code == 200
        assert client.delete(f"/api/presets/{preset_id}").status_code == 404

    def test_名前の検証(self, client):
        res = client.post("/api/presets", json={"name": "  ", "vocabulary": ""})
        assert res.status_code == 400


class TestSummaries:
    def _make_done_job(self):
        job_id = db.create_job("会議.mp3", "ja")
        db.add_segment(job_id, 0, 0.0, 2.0, "こんにちは", speaker=1)
        db.set_status(job_id, "done")
        return job_id

    def test_llm未設定なら503(self, client):
        job_id = self._make_done_job()
        res = client.post(f"/api/jobs/{job_id}/summaries", json={"kind": "summary"})
        assert res.status_code == 503
        res = client.post(
            "/api/summarize",
            json={"kind": "summary", "segments": [{"start": 0, "end": 1, "text": "a"}]},
        )
        assert res.status_code == 503

    def test_ジョブの要約生成と保存(self, client, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        monkeypatch.setattr(llm, "generate", lambda kind, segments, names: "生成結果")
        job_id = self._make_done_job()

        res = client.post(f"/api/jobs/{job_id}/summaries", json={"kind": "minutes"})
        assert res.status_code == 200
        assert res.json()["content"] == "生成結果"
        # 保存され、ジョブ詳細にも載る
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["summaries"]["minutes"]["content"] == "生成結果"

    def test_未完了ジョブは409(self, client, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        job_id = db.create_job("a.mp3", None)
        res = client.post(f"/api/jobs/{job_id}/summaries", json={"kind": "summary"})
        assert res.status_code == 409

    def test_未対応のkindは400(self, client, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        job_id = self._make_done_job()
        res = client.post(f"/api/jobs/{job_id}/summaries", json={"kind": "poem"})
        assert res.status_code == 400

    def test_summarizeはセグメントを検証する(self, client, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        res = client.post("/api/summarize", json={"kind": "summary", "segments": []})
        assert res.status_code == 400
        res = client.post(
            "/api/summarize", json={"kind": "summary", "segments": [{"bad": 1}]}
        )
        assert res.status_code == 400

    def test_summarizeで生成できる(self, client, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        monkeypatch.setattr(llm, "generate", lambda kind, segments, names: "要約テキスト")
        res = client.post(
            "/api/summarize",
            json={
                "kind": "summary",
                "segments": [{"start": 0, "end": 1, "text": "こんにちは", "speaker": 1}],
                "speaker_names": {"1": "山田"},
            },
        )
        assert res.status_code == 200
        assert res.json()["content"] == "要約テキスト"

    def test_llmエラーは502(self, client, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")

        def fail(kind, segments, names):
            raise llm.LLMError("接続できません")

        monkeypatch.setattr(llm, "generate", fail)
        job_id = self._make_done_job()
        res = client.post(f"/api/jobs/{job_id}/summaries", json={"kind": "summary"})
        assert res.status_code == 502

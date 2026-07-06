"""llm.py の入力整形・エラーハンドリングのテスト(実サーバーには接続しない)。"""

import json
import urllib.error

import pytest

from server import config, llm


def seg(start, end, text, speaker=None):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


class TestBuildTranscript:
    def test_時刻タグと氏名が反映される(self):
        text = llm.build_transcript([seg(0, 1, "こんにちは", 1)], {"1": "山田"})
        assert text == "[00:00:00] 山田: こんにちは\n"

    def test_長い粒度で結合される(self):
        text = llm.build_transcript(
            [seg(0, 1, "こんにちは", 1), seg(1.5, 2.5, "世界", 1)], None
        )
        assert text == "[00:00:00] 話者1: こんにちは世界\n"

    def test_上限を超えると切り詰められる(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_MAX_INPUT_CHARS", 50)
        segments = [seg(i, i + 1, "あ" * 30, i + 1) for i in range(10)]
        text = llm.build_transcript(segments, None)
        assert "打ち切られています" in text
        assert len(text) < 200


class TestGenerate:
    def test_未設定ならエラー(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "")
        with pytest.raises(llm.LLMError):
            llm.generate("summary", [seg(0, 1, "a")], None)

    def test_未対応のkindはエラー(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        with pytest.raises(llm.LLMError):
            llm.generate("poem", [seg(0, 1, "a")], None)

    def test_空のセグメントはエラー(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        with pytest.raises(llm.LLMError):
            llm.generate("summary", [], None)

    def test_正常応答(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": " 要約です "}}]}
                ).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data)
            return FakeResponse()

        monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
        result = llm.generate("summary", [seg(0, 1, "こんにちは")], None)
        assert result == "要約です"
        assert captured["url"] == "http://llm.test/v1/chat/completions"
        assert captured["payload"]["model"] == config.LLM_MODEL
        assert "こんにちは" in captured["payload"]["messages"][1]["content"]

    def test_接続失敗はLLMErrorに変換される(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")

        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(llm.LLMError, match="接続できません"):
            llm.generate("summary", [seg(0, 1, "a")], None)

    def test_不正な応答形式はLLMError(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_URL", "http://llm.test/v1")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"unexpected": true}'

        monkeypatch.setattr(
            llm.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse()
        )
        with pytest.raises(llm.LLMError, match="応答形式"):
            llm.generate("summary", [seg(0, 1, "a")], None)

"""realtime.py の暫定(partial)表示ロジックのテスト。

推論はモックし、送信タイミングの制御(間隔・ロック・クリア)だけを検証する。
"""

import time

import numpy as np
import pytest

from server import config, realtime, transcriber


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_PARTIAL_INTERVAL", 2.0)
    monkeypatch.setattr(
        transcriber,
        "transcribe_pcm",
        lambda audio, language, vocabulary=None, context=None: iter(
            [transcriber.Segment(0.0, 1.0, "こんにちは")]
        ),
    )
    s = realtime.RealtimeSession(FakeWS())
    s.buffer = np.zeros(realtime.SAMPLE_RATE, dtype=np.float32)  # 1秒ぶん
    s.last_partial = time.monotonic() - 10  # 前回送信から十分経過している状態
    return s


class TestMaybeSendPartial:
    @pytest.mark.anyio
    async def test_間隔が空いていればpartialを送る(self, session):
        await session._maybe_send_partial()
        assert session.ws.sent == [
            {"type": "partial", "start": 0.0, "text": "こんにちは"}
        ]
        assert session.partial_shown is True

    @pytest.mark.anyio
    async def test_バッファは消費しない(self, session):
        before = len(session.buffer)
        await session._maybe_send_partial()
        assert len(session.buffer) == before
        assert session.offset == 0.0

    @pytest.mark.anyio
    async def test_間隔内は送らない(self, session):
        session.last_partial = time.monotonic()
        await session._maybe_send_partial()
        assert session.ws.sent == []

    @pytest.mark.anyio
    async def test_無効化されていれば送らない(self, session, monkeypatch):
        monkeypatch.setattr(config, "REALTIME_PARTIAL_INTERVAL", 0.0)
        await session._maybe_send_partial()
        assert session.ws.sent == []

    @pytest.mark.anyio
    async def test_推論ロック使用中はスキップする(self, session):
        with transcriber.inference_lock:
            await session._maybe_send_partial()
        assert session.ws.sent == []

    @pytest.mark.anyio
    async def test_確定後に暫定表示がクリアされる(self, session, monkeypatch):
        # 表示中の partial がある状態で確定推論(結果なし)が走ると、
        # 空の partial でクライアントの表示を消す
        monkeypatch.setattr(
            transcriber,
            "transcribe_pcm",
            lambda audio, language, vocabulary=None, context=None: iter([]),
        )
        session.partial_shown = True
        await session._process(force=True)
        assert {"type": "partial", "start": 0.0, "text": ""} in session.ws.sent
        assert session.partial_shown is False


@pytest.fixture()
def anyio_backend():
    return "asyncio"

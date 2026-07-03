"""リアルタイム文字起こし(WebSocket)。

ブラウザから 16kHz mono int16 PCM のバイナリチャンクを受け取り、
Silero VAD で「発話が途切れた区間」を検出するたびに faster-whisper で
推論して、絶対時刻付きのセグメントを JSON で返す。

Whisper 自体はストリーミング非対応のため、この
「VAD で区切ってチャンク推論」方式を採用している。
"""

import asyncio
import json
import logging

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from faster_whisper.vad import VadOptions, get_speech_timestamps

from . import config, diarize, transcriber

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 発話終了とみなす無音時間(秒)
MIN_TRAILING_SILENCE = 0.6
# これを超えたら発話中でも強制的に推論する(秒)
MAX_BUFFER_SECONDS = 18.0
# VAD を回す間隔(バッファ増分・秒)
VAD_INTERVAL = 0.5

_sessions = 0
_sessions_lock = asyncio.Lock()

_vad_options = VadOptions(min_silence_duration_ms=400, speech_pad_ms=100)


class RealtimeSession:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.buffer = np.zeros(0, dtype=np.float32)
        self.offset = 0.0  # 消費済み音声の絶対時刻(秒)
        self.language: str | None = None
        self.vocabulary: str | None = None
        self.context: str | None = None
        self.since_vad = 0.0
        self.tracker = diarize.SpeakerTracker()

    async def run(self) -> None:
        await self.ws.send_json({"type": "ready", "model": config.MODEL_NAME})
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if (data := message.get("bytes")) is not None:
                self._append_pcm(data)
                self.since_vad += len(data) / 2 / SAMPLE_RATE
                if self.since_vad >= VAD_INTERVAL:
                    self.since_vad = 0.0
                    await self._process(force=False)
            elif (text := message.get("text")) is not None:
                await self._handle_control(text)

    def _append_pcm(self, data: bytes) -> None:
        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        self.buffer = np.concatenate([self.buffer, pcm])

    async def _handle_control(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "config":
            self.language = msg.get("language") or None
            self.vocabulary = (msg.get("vocabulary") or "")[:1000] or None
            self.context = (msg.get("context") or "")[:1000] or None
        elif msg.get("type") == "pause":
            # 一時停止: ここまでのバッファを確定させて返す
            await self._process(force=True)
            await self.ws.send_json({"type": "paused"})
        elif msg.get("type") == "resume":
            # 再開: 停止していた実時間ぶんオフセットを進め、
            # 時刻タグを録音開始からの実時間に合わせる
            try:
                gap = float(msg.get("gap") or 0)
            except (TypeError, ValueError):
                gap = 0.0
            if 0 < gap < 86400:
                self.offset += gap
        elif msg.get("type") == "stop":
            await self._process(force=True)
            await self.ws.send_json({"type": "done"})

    async def _process(self, force: bool) -> None:
        buf_seconds = len(self.buffer) / SAMPLE_RATE
        if buf_seconds < 0.3:
            return

        cut: int | None = None  # 推論対象とするバッファ末尾位置(サンプル)
        if force or buf_seconds >= MAX_BUFFER_SECONDS:
            cut = len(self.buffer)
        else:
            speech = get_speech_timestamps(self.buffer, _vad_options)
            if not speech:
                # 無音のみ。末尾 0.5 秒だけ残して捨てる(次の発話の頭を保護)
                keep = int(0.5 * SAMPLE_RATE)
                if len(self.buffer) > keep:
                    self._consume(len(self.buffer) - keep)
                return
            last_end = speech[-1]["end"]
            trailing = (len(self.buffer) - last_end) / SAMPLE_RATE
            if trailing >= MIN_TRAILING_SILENCE:
                cut = last_end

        if cut is None:
            return

        chunk = self.buffer[:cut].copy()
        base = self.offset
        self._consume(cut)
        segments = await asyncio.to_thread(
            lambda: list(
                transcriber.transcribe_pcm(
                    chunk, self.language, self.vocabulary, self.context
                )
            )
        )
        for seg in segments:
            # assign は内部でモデルの遅延ロードを含むためスレッドで実行
            # (無効時は None が返る)
            clip = chunk[int(seg.start * SAMPLE_RATE):int(seg.end * SAMPLE_RATE)]
            speaker = await asyncio.to_thread(self.tracker.assign, clip)
            await self.ws.send_json(
                {
                    "type": "segment",
                    "start": round(base + seg.start, 2),
                    "end": round(base + seg.end, 2),
                    "text": seg.text,
                    "speaker": speaker,
                }
            )

    def _consume(self, samples: int) -> None:
        self.offset += samples / SAMPLE_RATE
        self.buffer = self.buffer[samples:]


async def handle_websocket(ws: WebSocket) -> None:
    global _sessions
    await ws.accept()

    async with _sessions_lock:
        if _sessions >= config.MAX_REALTIME_SESSIONS:
            await ws.send_json(
                {"type": "error", "message": "同時接続数の上限に達しています。しばらく待ってから再接続してください。"}
            )
            await ws.close()
            return
        _sessions += 1

    try:
        await RealtimeSession(ws).run()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("realtime session error")
    finally:
        async with _sessions_lock:
            _sessions -= 1

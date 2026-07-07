"""リアルタイム文字起こし(WebSocket: /ws/realtime)。

ブラウザから 16kHz mono int16 PCM のバイナリチャンクを受け取り、
Silero VAD で「発話が途切れた区間」を検出するたびに faster-whisper で
推論して、絶対時刻付きのセグメントを JSON で返す。

Whisper 自体はストリーミング非対応のため、この
「VAD で区切ってチャンク推論」方式を採用している。

## WebSocket プロトコル

クライアント → サーバー:
- バイナリフレーム: 16kHz mono int16 PCM の音声チャンク
- テキストフレーム(JSON):
    {"type": "config", "language": "ja", "vocabulary": "...", "context": "..."}
        認識設定。最初の音声送信前に送る想定(途中変更も可)
    {"type": "pause"}   一時停止。ここまでのバッファを確定して返す
    {"type": "resume", "gap": 秒}
        再開。gap は一時停止していた実時間で、以降の時刻タグに加算される
    {"type": "stop"}    終了。残バッファを確定し "done" を返す

サーバー → クライアント(すべて JSON):
    {"type": "ready", "model": "..."}    接続受理(モデル名つき)
    {"type": "segment", "start": 1.2, "end": 3.4, "text": "...", "speaker": 1}
        確定した文字起こし。start/end は録音開始からの実時間(秒)、
        speaker は話者番号(話者分離が無効なら null)
    {"type": "partial", "start": 1.2, "text": "..."}
        発話中の暫定テキスト(REALTIME_PARTIAL_INTERVAL 秒ごと・ベストエフォート)。
        次の partial または segment で置き換える。text が空文字なら表示を消す。
        確定ではないため保存対象にしないこと
    {"type": "paused"}                   pause の完了通知
    {"type": "done"}                     stop の完了通知(この後クローズ想定)
    {"type": "error", "message": "..."}  受理拒否・推論エラーなど(送信後クローズ)

対応するクライアント実装は web/src/Recorder.tsx。
"""

import asyncio
import json
import logging
import time

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from faster_whisper.vad import VadOptions, get_speech_timestamps

from . import config, diarize, exporters, transcriber

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
    """1つの WebSocket 接続に対応する文字起こしセッション。

    音声はいったん self.buffer に溜め、VAD が「発話の終わり」を検出した
    時点でバッファ先頭からその位置までを切り出して推論する。
    self.offset は「これまでに切り出し済みの音声の合計時間 + 一時停止時間」
    で、セグメントの相対時刻に足すことで録音開始からの実時間になる。
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.buffer = np.zeros(0, dtype=np.float32)
        self.offset = 0.0  # 消費済み音声の絶対時刻(秒)
        self.language: str | None = None
        self.vocabulary: str | None = None
        self.context: str | None = None
        self.since_vad = 0.0  # 前回 VAD 実行以降に受信した音声量(秒)
        self.last_partial = 0.0  # 前回 partial を送った時刻(monotonic)
        self.partial_shown = False  # クライアントに partial が表示されているか
        self.tracker = diarize.SpeakerTracker()

    async def run(self) -> None:
        """受信ループ。切断まで音声チャンクと制御メッセージを処理し続ける。"""
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
        """int16 PCM のバイト列を float32 (-1.0〜1.0) に変換してバッファへ追加。"""
        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        self.buffer = np.concatenate([self.buffer, pcm])

    async def _handle_control(self, text: str) -> None:
        """テキストフレーム(JSON 制御メッセージ)を処理する。不正な JSON は無視。"""
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
        """バッファを調べ、確定できる区間があれば推論してセグメントを送信する。

        force=True(stop/pause 時)はバッファ全体を即座に推論する。
        force=False は次の順で判定する:
          1. バッファが MAX_BUFFER_SECONDS を超えた → 長い発話の途中でも全体を推論
          2. VAD で最後の発話終了後に MIN_TRAILING_SILENCE 以上の無音がある
             → 発話終了とみなし、そこまでを推論
          3. 無音のみ → 末尾 0.5 秒だけ残して破棄(次の発話の頭を欠かさないため)
          4. 発話が続いている → 何もしない(次回の呼び出しで再判定)
        """
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
            # 発話継続中: 確定はできないが、間隔が空いていれば暫定テキストを送る
            await self._maybe_send_partial()
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
        # 確定を送ったので暫定表示は不要になった。セグメントが1つも出なかった
        # 場合(ノイズのみ等)も、残った暫定表示を消す
        if self.partial_shown:
            await self._send_partial(0.0, "")
        self.last_partial = time.monotonic()

    async def _maybe_send_partial(self) -> None:
        """発話継続中のバッファを推論して暫定テキストを送る(ベストエフォート)。

        確定推論を妨げないよう、次の場合はスキップする:
          - 無効化されている(REALTIME_PARTIAL_INTERVAL <= 0)
          - 前回の partial から間隔が空いていない
          - 推論ロックが使用中(確定側・他セッションを優先)
        バッファは消費しない。次の partial か確定セグメントで置き換えられる。
        """
        interval = config.REALTIME_PARTIAL_INTERVAL
        if interval <= 0:
            return
        if time.monotonic() - self.last_partial < interval:
            return
        if transcriber.inference_lock.locked():
            return
        chunk = self.buffer.copy()
        base = self.offset
        segments = await asyncio.to_thread(
            lambda: list(
                transcriber.transcribe_pcm(
                    chunk, self.language, self.vocabulary, self.context
                )
            )
        )
        text = ""
        for seg in segments:
            text = exporters._join_text(text, seg.text)
        self.last_partial = time.monotonic()
        if text or self.partial_shown:
            await self._send_partial(base, text)

    async def _send_partial(self, start: float, text: str) -> None:
        await self.ws.send_json(
            {"type": "partial", "start": round(start, 2), "text": text}
        )
        self.partial_shown = bool(text)

    def _consume(self, samples: int) -> None:
        """バッファ先頭 samples 個を消費済みにし、絶対時刻オフセットを進める。"""
        self.offset += samples / SAMPLE_RATE
        self.buffer = self.buffer[samples:]


async def handle_websocket(ws: WebSocket) -> None:
    """WebSocket 接続の受理・同時セッション数の制限・後始末を行う入口。"""
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
    except Exception as e:
        logger.exception("realtime session error")
        # 推論エラー(GPU のメモリ不足など)を黙って切断せず、原因と対処法を
        # クライアントに伝えてから閉じる(切断済みなら送信失敗を握りつぶす)
        try:
            await ws.send_json(
                {
                    "type": "error",
                    "message": "文字起こしに失敗しました: "
                    + transcriber.explain_inference_error(e),
                }
            )
            await ws.close()
        except Exception:
            pass
    finally:
        async with _sessions_lock:
            _sessions -= 1

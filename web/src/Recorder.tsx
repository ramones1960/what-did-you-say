/**
 * リアルタイム文字起こし画面。
 *
 * 音声の流れ:
 *   getUserMedia (マイク)
 *     → AudioWorklet (public/pcm-worklet.js で 16kHz int16 PCM に変換)
 *     → WebSocket /ws/realtime へバイナリ送信
 *     → サーバーから segment メッセージ (時刻・話者付き) を受信して表示
 *
 * サーバー側のプロトコル定義は server/realtime.py のモジュール docstring 参照。
 *
 * 状態遷移:
 *   idle → connecting → recording ⇄ paused → finishing → idle
 *   - pause: AudioContext を suspend して送信を止め、{"type":"pause"} を送る
 *   - resume: 停止していた実時間 (gap) をサーバーへ伝えて時刻タグを補正
 *   - stop: {"type":"stop"} を送り、サーバーの "done" を待ってから idle に戻る
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  GRANULARITIES,
  Granularity,
  LANGUAGES,
  Segment,
  SpeakerNames,
  downloadText,
  hms,
  loadGranularity,
  mergeSegments,
  saveGranularity,
  speakerLabel,
  uniqueSpeakers,
  wsUrl,
} from "./lib";
import PromptSettings, { usePromptSettings } from "./PromptSettings";
import SpeakerNamesPanel from "./SpeakerNames";

type Status = "idle" | "connecting" | "recording" | "paused" | "finishing";

export default function Recorder() {
  const [status, setStatus] = useState<Status>("idle");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [error, setError] = useState("");
  const [language, setLanguage] = useState("ja");
  const [elapsed, setElapsed] = useState(0);
  const [prompt, setPrompt] = usePromptSettings();
  const [names, setNames] = useState<SpeakerNames>({});
  const [granularity, setGranularity] = useState<Granularity>(loadGranularity);

  // 表示・保存用に結合したセグメント(元データは segments に細かいまま残る)
  const displaySegments = useMemo(
    () => mergeSegments(segments, granularity),
    [segments, granularity],
  );

  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number>(0);
  const pausedAtRef = useRef<number>(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [segments]);

  useEffect(() => () => cleanup(), []);

  /** マイク・AudioContext・WebSocket をすべて閉じる(アンマウント時と開始失敗時)。 */
  function cleanup() {
    window.clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
  }

  /** 録音開始: WebSocket 接続 → 設定送信 → マイク音声のストリーミングを開始。 */
  async function start() {
    setError("");
    setSegments([]);
    setElapsed(0);
    setStatus("connecting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      streamRef.current = stream;

      const ws = new WebSocket(wsUrl("/ws/realtime"));
      wsRef.current = ws;

      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "segment") {
          setSegments((prev) => [...prev, msg]);
        } else if (msg.type === "error") {
          setError(msg.message);
          stop();
        } else if (msg.type === "done") {
          ws.close();
          setStatus("idle");
        }
      };
      ws.onerror = () => setError("サーバーとの接続に失敗しました");
      ws.onclose = () => {
        setStatus((prev) => (prev === "idle" ? prev : "idle"));
      };

      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        ws.onclose = () => reject(new Error("接続できませんでした"));
      });
      ws.send(
        JSON.stringify({
          type: "config",
          language,
          vocabulary: prompt.vocabulary,
          context: prompt.context,
        }),
      );

      const ctx = new AudioContext();
      ctxRef.current = ctx;
      await ctx.audioWorklet.addModule("/pcm-worklet.js");
      const source = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, "pcm-worklet");
      node.port.onmessage = (ev) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(ev.data);
      };
      source.connect(node);

      setStatus("recording");
      const startedAt = Date.now();
      timerRef.current = window.setInterval(
        () => setElapsed((Date.now() - startedAt) / 1000),
        500,
      );
    } catch (e) {
      cleanup();
      setStatus("idle");
      setError(e instanceof Error ? e.message : "マイクを開始できませんでした");
    }
  }

  function pause() {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    pausedAtRef.current = Date.now();
    // AudioContext を止めるとマイクからの PCM 送信が止まる
    ctxRef.current?.suspend().catch(() => {});
    ws.send(JSON.stringify({ type: "pause" }));
    setStatus("paused");
  }

  function resume() {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const gap = (Date.now() - pausedAtRef.current) / 1000;
    // 停止していた実時間を伝え、再開後の時刻タグを録音開始からの実時間に揃える
    ws.send(JSON.stringify({ type: "resume", gap }));
    ctxRef.current?.resume().catch(() => {});
    setStatus("recording");
  }

  /** 停止: マイクを閉じて stop を送り、サーバーが残バッファを確定するのを待つ。 */
  function stop() {
    window.clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      // 残りのバッファをサーバー側で flush してもらう。
      // "done" を受信したら onmessage 側で idle に戻る
      ws.send(JSON.stringify({ type: "stop" }));
      setStatus("finishing");
    } else {
      setStatus("idle");
    }
  }

  const recording = status === "recording";

  return (
    <section>
      <div className="toolbar">
        <select
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          disabled={status !== "idle"}
        >
          {LANGUAGES.map(([code, label]) => (
            <option key={code} value={code}>
              {label}
            </option>
          ))}
        </select>
        {status === "idle" ? (
          <button className="primary" onClick={start}>
            録音を開始
          </button>
        ) : (
          <>
            {recording && <button onClick={pause}>一時停止</button>}
            {status === "paused" && (
              <button className="primary" onClick={resume}>
                再開
              </button>
            )}
            <button onClick={stop} disabled={status === "finishing"}>
              {status === "finishing" ? "処理中…" : "停止"}
            </button>
          </>
        )}
        {recording && <span className="rec-indicator">録音中 {hms(elapsed)}</span>}
        {status === "paused" && <span className="status-note">一時停止中 {hms(elapsed)}</span>}
        {status === "connecting" && <span className="status-note">接続中…</span>}
        <span className="spacer" />
        <label className="inline-field">
          発言の区切り
          <select
            value={granularity}
            onChange={(e) => {
              const v = e.target.value as Granularity;
              setGranularity(v);
              saveGranularity(v);
            }}
          >
            {GRANULARITIES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={() =>
            downloadText(
              "transcript.txt",
              displaySegments
                .map((s) => {
                  const label = speakerLabel(s.speaker, names);
                  return `[${hms(s.start)}] ${label ? `${label}: ` : ""}${s.text}`;
                })
                .join("\n") + "\n",
            )
          }
          disabled={segments.length === 0}
        >
          テキストを保存 (TXT)
        </button>
      </div>

      <PromptSettings values={prompt} onChange={setPrompt} disabled={status !== "idle"} />

      <SpeakerNamesPanel
        speakers={uniqueSpeakers(segments)}
        names={names}
        onChange={setNames}
      />

      {error && <p className="error">{error}</p>}

      <div className="transcript" ref={listRef}>
        {segments.length === 0 && (
          <p className="empty-state">
            「録音を開始」を押すとマイクの使用許可を求められます。話した内容は発話の区切りごとに、時刻・話者タグ付きでここに表示されます。
          </p>
        )}
        {displaySegments.map((s, i) => (
          <p key={i}>
            <span className="ts">[{hms(s.start)}]</span>
            {s.speaker != null && (
              <span className={`spk spk-c${(s.speaker - 1) % 6}`}>
                {speakerLabel(s.speaker, names)}
              </span>
            )}
            {s.text}
          </p>
        ))}
      </div>
    </section>
  );
}

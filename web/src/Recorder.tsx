import { useEffect, useRef, useState } from "react";
import {
  LANGUAGES,
  Segment,
  SpeakerNames,
  downloadText,
  hms,
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

  function cleanup() {
    window.clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
  }

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

  function stop() {
    window.clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      // 残りのバッファをサーバー側で flush してもらう
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
            ● 録音開始
          </button>
        ) : (
          <>
            {recording && <button onClick={pause}>⏸ 一時停止</button>}
            {status === "paused" && (
              <button className="primary" onClick={resume}>
                ▶ 再開
              </button>
            )}
            <button onClick={stop} disabled={status === "finishing"}>
              {status === "finishing" ? "処理中…" : "■ 停止"}
            </button>
          </>
        )}
        {recording && <span className="rec-indicator">録音中 {hms(elapsed)}</span>}
        {status === "paused" && <span className="muted">一時停止中 {hms(elapsed)}</span>}
        {status === "connecting" && <span className="muted">接続中…</span>}
        <span className="spacer" />
        <button
          onClick={() =>
            downloadText(
              "transcript.txt",
              segments
                .map((s) => {
                  const label = speakerLabel(s.speaker, names);
                  return `[${hms(s.start)}] ${label ? `${label}: ` : ""}${s.text}`;
                })
                .join("\n") + "\n",
            )
          }
          disabled={segments.length === 0}
        >
          TXT ダウンロード
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
          <p className="muted">
            マイクに向かって話すと、発話の区切りごとに時刻付きで文字起こしされます。
          </p>
        )}
        {segments.map((s, i) => (
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

import { useCallback, useEffect, useRef, useState } from "react";
import { Job, LANGUAGES, hms } from "./lib";

export default function Upload() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [active, setActive] = useState<Job | null>(null);
  const [language, setLanguage] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const pollRef = useRef(0);

  const refreshJobs = useCallback(async () => {
    const res = await fetch("/api/jobs");
    if (res.ok) setJobs(await res.json());
  }, []);

  useEffect(() => {
    refreshJobs();
  }, [refreshJobs]);

  // アクティブなジョブが処理中の間はポーリングして進捗とセグメントを取り込む
  useEffect(() => {
    window.clearInterval(pollRef.current);
    if (!active || (active.status !== "queued" && active.status !== "processing")) {
      return;
    }
    pollRef.current = window.setInterval(async () => {
      const from = active.segments?.length ?? 0;
      const res = await fetch(`/api/jobs/${active.id}?segments_from=${from}`);
      if (!res.ok) return;
      const fresh: Job = await res.json();
      setActive((prev) =>
        prev && prev.id === fresh.id
          ? { ...fresh, segments: [...(prev.segments ?? []), ...(fresh.segments ?? [])] }
          : prev,
      );
      if (fresh.status === "done" || fresh.status === "error") refreshJobs();
    }, 1500);
    return () => window.clearInterval(pollRef.current);
  }, [active?.id, active?.status, active?.segments?.length, refreshJobs]);

  async function upload(file: File) {
    setError("");
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`/api/jobs?language=${encodeURIComponent(language)}`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      setError(body?.detail ?? `アップロードに失敗しました (${res.status})`);
      return;
    }
    const { id } = await res.json();
    await refreshJobs();
    await openJob(id);
  }

  async function openJob(id: string) {
    const res = await fetch(`/api/jobs/${id}`);
    if (res.ok) setActive(await res.json());
  }

  async function removeJob(id: string) {
    await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    if (active?.id === id) setActive(null);
    refreshJobs();
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) upload(file);
  }

  const statusLabel: Record<Job["status"], string> = {
    queued: "待機中",
    processing: "処理中",
    done: "完了",
    error: "エラー",
  };

  return (
    <section>
      <div className="toolbar">
        <select value={language} onChange={(e) => setLanguage(e.target.value)}>
          {LANGUAGES.map(([code, label]) => (
            <option key={code} value={code}>
              {label}
            </option>
          ))}
        </select>
        <label className="primary button">
          ファイルを選択
          <input
            type="file"
            accept="audio/*,video/*,.m4a,.mp3,.wav,.mp4,.mov,.mkv,.webm"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload(f);
              e.target.value = "";
            }}
          />
        </label>
      </div>

      <div
        className={`dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        音声・動画ファイルをここにドラッグ&ドロップ
      </div>

      {error && <p className="error">{error}</p>}

      {jobs.length > 0 && (
        <table className="jobs">
          <thead>
            <tr>
              <th>ファイル</th>
              <th>状態</th>
              <th>言語</th>
              <th>長さ</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr
                key={j.id}
                className={active?.id === j.id ? "active" : ""}
                onClick={() => openJob(j.id)}
              >
                <td>{j.filename}</td>
                <td>
                  {statusLabel[j.status]}
                  {j.status === "processing" && ` ${Math.round(j.progress * 100)}%`}
                </td>
                <td>{j.language ?? "-"}</td>
                <td>{j.duration ? hms(j.duration) : "-"}</td>
                <td>
                  {j.status !== "processing" && j.status !== "queued" && (
                    <button
                      className="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeJob(j.id);
                      }}
                    >
                      削除
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {active && (
        <div className="result">
          <div className="toolbar">
            <strong>{active.filename}</strong>
            {(active.status === "processing" || active.status === "queued") && (
              <progress value={active.progress} max={1} />
            )}
            <span className="spacer" />
            {(["txt", "srt", "vtt", "json"] as const).map((fmt) => (
              <a
                key={fmt}
                className="button small"
                href={`/api/jobs/${active.id}/export?format=${fmt}`}
              >
                {fmt.toUpperCase()}
              </a>
            ))}
          </div>
          {active.status === "error" && <p className="error">{active.error}</p>}
          <div className="transcript">
            {(active.segments ?? []).map((s) => (
              <p key={s.start + s.text}>
                <span className="ts">[{hms(s.start)}]</span> {s.text}
              </p>
            ))}
            {active.status === "done" && (active.segments ?? []).length === 0 && (
              <p className="muted">音声から発話を検出できませんでした。</p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

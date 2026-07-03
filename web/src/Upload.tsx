/**
 * ファイルから文字起こしする画面。
 *
 * 流れ:
 *   1. ファイル選択 or ドラッグ&ドロップ → POST /api/jobs (multipart)
 *   2. 返ってきたジョブ ID を「表示中ジョブ (active)」として開く
 *   3. 処理中は GET /api/jobs/{id}?segments_from=N を1.5秒間隔でポーリングし、
 *      新しく確定したセグメントだけを差分で受け取って追記する
 *   4. 完了後は話者の氏名設定 (PUT /api/jobs/{id}/speakers) と
 *      エクスポート (GET /api/jobs/{id}/export?format=...) が使える
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  GRANULARITIES,
  Granularity,
  Job,
  LANGUAGES,
  SpeakerNames,
  hms,
  loadGranularity,
  mergeSegments,
  saveGranularity,
  speakerLabel,
  uniqueSpeakers,
} from "./lib";
import PromptSettings, { usePromptSettings } from "./PromptSettings";
import SpeakerNamesPanel from "./SpeakerNames";

export default function Upload() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [active, setActive] = useState<Job | null>(null);
  const [language, setLanguage] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [prompt, setPrompt] = usePromptSettings();
  const [names, setNames] = useState<SpeakerNames>({});
  const [savingNames, setSavingNames] = useState(false);
  const [granularity, setGranularity] = useState<Granularity>(loadGranularity);
  const pollRef = useRef(0);

  // 表示用に結合したセグメント(元データは active.segments に細かいまま残る)
  const displaySegments = useMemo(
    () => mergeSegments(active?.segments ?? [], granularity),
    [active?.segments, granularity],
  );

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

  /** ファイルをアップロードしてジョブを作成し、その結果画面を開く。 */
  async function upload(file: File) {
    setError("");
    const form = new FormData();
    form.append("file", file);
    form.append("vocabulary", prompt.vocabulary);
    form.append("context", prompt.context);
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

  /** ジョブを開いて表示する。保存済みの話者氏名マッピングも復元する。 */
  async function openJob(id: string) {
    const res = await fetch(`/api/jobs/${id}`);
    if (!res.ok) return;
    const job: Job = await res.json();
    setActive(job);
    try {
      setNames(JSON.parse(job.speaker_names || "{}"));
    } catch {
      setNames({});
    }
  }

  /** 話者氏名マッピングをサーバーに保存する(エクスポートに反映される)。 */
  async function saveNames() {
    if (!active) return;
    setSavingNames(true);
    try {
      const res = await fetch(`/api/jobs/${active.id}/speakers`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names }),
      });
      if (!res.ok) setError("氏名の保存に失敗しました");
    } finally {
      setSavingNames(false);
    }
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

      <PromptSettings values={prompt} onChange={setPrompt} />

      <div
        className={`dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <strong>音声・動画ファイルをここにドラッグ&ドロップ</strong>
        <span className="hint">
          対応形式: mp3 / wav / m4a / flac / mp4 / mov / mkv / webm など。動画は音声のみを抽出して処理します。
        </span>
      </div>

      {error && <p className="error">{error}</p>}

      {jobs.length > 0 && (
        <table className="jobs">
          <thead>
            <tr>
              <th>ファイル名</th>
              <th>状態</th>
              <th>言語</th>
              <th>長さ</th>
              <th>操作</th>
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
            {active.status === "done" && (
              <span className="status-note">ダウンロード:</span>
            )}
            {/* 発言の区切りは TXT のみに反映(SRT/VTT は字幕用に細かいまま) */}
            {(["txt", "srt", "vtt", "json"] as const).map((fmt) => (
              <a
                key={fmt}
                className="button small"
                href={`/api/jobs/${active.id}/export?format=${fmt}&granularity=${granularity}`}
              >
                {fmt.toUpperCase()}
              </a>
            ))}
          </div>
          {active.status === "error" && <p className="error">{active.error}</p>}
          <SpeakerNamesPanel
            speakers={uniqueSpeakers(active.segments ?? [])}
            names={names}
            onChange={setNames}
            onSave={saveNames}
            saving={savingNames}
          />
          <div className="transcript">
            {displaySegments.map((s) => (
              <p key={s.start + s.text}>
                <span className="ts">[{hms(s.start)}]</span>
                {s.speaker != null && (
                  <span className={`spk spk-c${(s.speaker - 1) % 6}`}>
                    {speakerLabel(s.speaker, names)}
                  </span>
                )}
                {s.text}
              </p>
            ))}
            {active.status === "done" && (active.segments ?? []).length === 0 && (
              <p className="empty-state">
                音声から発話を検出できませんでした。無音のファイルでないかご確認ください。
              </p>
            )}
            {(active.status === "processing" || active.status === "queued") &&
              (active.segments ?? []).length === 0 && (
                <p className="empty-state">
                  処理中です。文字起こしされたテキストから順にここに表示されます。
                </p>
              )}
          </div>
        </div>
      )}
    </section>
  );
}

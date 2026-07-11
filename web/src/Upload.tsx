/**
 * ファイルから文字起こしする画面。
 *
 * 流れ:
 *   1. ファイル選択 or ドラッグ&ドロップ → 選択中ファイルとして保留(まだ送らない)
 *   2. 言語・用語リスト等を設定してから「文字起こしを開始」→ POST /api/jobs
 *   3. 返ってきたジョブ ID を「表示中ジョブ (active)」として開く
 *   4. 処理中は GET /api/jobs/{id}?segments_from=N を1.5秒間隔でポーリングし、
 *      新しく確定したセグメントだけを差分で受け取って追記する。
 *      途中で「中断」→ POST /api/jobs/{id}/cancel で打ち切れる
 *   5. 完了後は話者の氏名設定 (PUT /api/jobs/{id}/speakers)、
 *      エクスポート (GET /api/jobs/{id}/export?format=...)、
 *      頻出単語の集計 (GET /api/jobs/{id}/words) が使える
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  GRANULARITIES,
  Granularity,
  Job,
  LANGUAGES,
  LlmInfo,
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
import SummaryPanel from "./SummaryPanel";
import WordFrequencies from "./WordFrequencies";

const MAX_PROMPT_CHARS = 1000;

export default function Upload({ llm }: { llm: LlmInfo | null }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [active, setActive] = useState<Job | null>(null);
  const [language, setLanguage] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [pending, setPending] = useState<File | null>(null); // 開始待ちのファイル
  // 話者の人数の幅(参加者全員が発話するとは限らないため最小〜最大で指定できる。
  // 空欄は自動推定、同数にすると人数固定、片方だけの指定も可)
  const [minSpeakers, setMinSpeakers] = useState("");
  const [maxSpeakers, setMaxSpeakers] = useState("");
  const [uploading, setUploading] = useState(false);
  const [canceling, setCanceling] = useState<string[]>([]); // 中断要求中のジョブ ID
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
    if (!res.ok) return;
    const fresh: Job[] = await res.json();
    setJobs(fresh);
    // 実行が終わった(=中断が反映された)ジョブは「中断中」表示から外す
    const running = new Set(
      fresh.filter((j) => j.status === "queued" || j.status === "processing").map((j) => j.id),
    );
    setCanceling((ids) => ids.filter((id) => running.has(id)));
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

  /** 選択中のファイルをアップロードしてジョブを作成し、その結果画面を開く。 */
  async function startTranscription() {
    if (!pending || uploading) return;
    setError("");
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", pending);
      form.append("vocabulary", prompt.vocabulary);
      form.append("context", prompt.context);
      if (minSpeakers) form.append("min_speakers", minSpeakers);
      if (maxSpeakers) form.append("max_speakers", maxSpeakers);
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
      setPending(null); // 開始したので選択を解除
      await refreshJobs();
      await openJob(id);
    } finally {
      setUploading(false);
    }
  }

  /** 処理中・待機中のジョブを中断する。途中までの結果は残る。 */
  async function cancelJob(id: string) {
    setCanceling((ids) => (ids.includes(id) ? ids : [...ids, id]));
    const res = await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
    if (!res.ok) {
      setError("中断に失敗しました");
      setCanceling((ids) => ids.filter((x) => x !== id));
      return;
    }
    await refreshJobs();
    // 表示中ジョブなら最新状態を取り込む(ポーリングも状態に応じて止まる)
    if (active?.id === id) {
      const fresh = await fetch(`/api/jobs/${id}`);
      if (fresh.ok) setActive(await fresh.json());
    }
  }

  /** 頻出単語を用語リストに追記する(重複・文字数上限を考慮)。 */
  function addVocabularyWord(word: string) {
    const terms = new Set(
      prompt.vocabulary.split(/[\n,、]/).map((t) => t.trim()).filter(Boolean),
    );
    if (terms.has(word)) return;
    const base = prompt.vocabulary.replace(/\s+$/, "");
    const next = base ? `${base}\n${word}` : word;
    if (next.length > MAX_PROMPT_CHARS) {
      setError(`用語リストが${MAX_PROMPT_CHARS}文字の上限に達しています`);
      return;
    }
    setError("");
    setPrompt({ ...prompt, vocabulary: next });
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
    if (file) {
      setError("");
      setPending(file);
    }
  }

  const statusLabel: Record<Job["status"], string> = {
    queued: "待機中",
    processing: "処理中",
    done: "完了",
    error: "エラー",
    canceled: "中断",
  };

  const isRunning = (s: Job["status"]) => s === "queued" || s === "processing";

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
        {/* 話者分離のヒント。幅で指定でき、同数にすると人数固定、空欄なら自動推定 */}
        <label className="inline-field">
          話者の人数
          <input
            type="number"
            min={1}
            max={16}
            placeholder="自動"
            className="num-speakers"
            value={minSpeakers}
            onChange={(e) => setMinSpeakers(e.target.value)}
          />
          〜
          <input
            type="number"
            min={1}
            max={16}
            placeholder="自動"
            className="num-speakers"
            value={maxSpeakers}
            onChange={(e) => setMaxSpeakers(e.target.value)}
          />
        </label>
        <label className="button">
          ファイルを選択
          <input
            type="file"
            accept="audio/*,video/*,.m4a,.mp3,.wav,.mp4,.mov,.mkv,.webm"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) {
                setError("");
                setPending(f);
              }
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
          対応形式: mp3 / wav / m4a / flac / mp4 / mov / mkv / webm など。動画は音声のみを抽出して処理します。ファイルを選ぶと下に開始ボタンが出ます。
        </span>
      </div>

      {pending && (
        <div className="pending-file">
          <span className="pending-name">
            選択中: <strong>{pending.name}</strong>
          </span>
          <span className="spacer" />
          <button
            className="primary"
            onClick={startTranscription}
            disabled={uploading}
          >
            {uploading ? "アップロード中…" : "文字起こしを開始"}
          </button>
          <button className="small" onClick={() => setPending(null)} disabled={uploading}>
            クリア
          </button>
        </div>
      )}

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
                  {isRunning(j.status) ? (
                    <button
                      className="small"
                      disabled={canceling.includes(j.id)}
                      onClick={(e) => {
                        e.stopPropagation();
                        cancelJob(j.id);
                      }}
                    >
                      {canceling.includes(j.id) ? "中断中…" : "中断"}
                    </button>
                  ) : (
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
            {isRunning(active.status) && <progress value={active.progress} max={1} />}
            {isRunning(active.status) && (
              <button
                className="small"
                disabled={canceling.includes(active.id)}
                onClick={() => cancelJob(active.id)}
              >
                {canceling.includes(active.id) ? "中断中…" : "中断"}
              </button>
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
          {active.status === "canceled" && (
            <p className="status-note">
              この文字起こしは中断されました。ここまでの結果は保存されています。
            </p>
          )}
          <SpeakerNamesPanel
            speakers={uniqueSpeakers(active.segments ?? [])}
            names={names}
            onChange={setNames}
            onSave={saveNames}
            saving={savingNames}
          />
          {/* 頻出単語の集計。用語リストへ追加して再アップロードすると精度が上がる */}
          {(active.status === "done" || active.status === "canceled") &&
            (active.segments ?? []).length > 0 && (
              <WordFrequencies
                jobId={active.id}
                vocabulary={prompt.vocabulary}
                onAddWord={addVocabularyWord}
              />
            )}
          {/* 完了後にローカル LLM で要約・議事録を生成(結果はジョブに保存される) */}
          {active.status === "done" && (
            <SummaryPanel
              llm={llm}
              segments={active.segments ?? []}
              names={names}
              jobId={active.id}
              initial={active.summaries}
              filenameBase={active.filename.replace(/\.[^.]+$/, "") || "transcript"}
            />
          )}
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

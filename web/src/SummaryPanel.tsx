/**
 * ローカル LLM による要約・議事録の生成パネル。
 *
 * リアルタイム画面・ファイル画面の両方から使う共通コンポーネント。
 * - jobId あり(ファイル画面): POST /api/jobs/{id}/summaries → サーバーに保存される
 * - jobId なし(リアルタイム画面): POST /api/summarize にセグメントを直接渡す(保存なし)
 *
 * LLM 連携(LLM_API_URL)が未設定のときは何も表示しない。
 */
import { useEffect, useState } from "react";
import { LlmInfo, Segment, SpeakerNames, SummaryResult, downloadText } from "./lib";

const KIND_LABELS: [string, string][] = [
  ["summary", "要約"],
  ["minutes", "議事録"],
];

interface Props {
  llm: LlmInfo | null;
  segments: Segment[];
  names: SpeakerNames;
  /** あればジョブ紐付けで生成・保存する(ファイル画面)。なければ保存なし(リアルタイム画面) */
  jobId?: string;
  /** ジョブに保存済みの生成結果(画面を開いたときの初期表示) */
  initial?: Record<string, SummaryResult>;
  /** ファイル名の元(ダウンロード時に「元名-要約.md」のようにする) */
  filenameBase: string;
}

export default function SummaryPanel({
  llm,
  segments,
  names,
  jobId,
  initial,
  filenameBase,
}: Props) {
  const [results, setResults] = useState<Record<string, SummaryResult>>({});
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState("");

  // ジョブを切り替えたら保存済みの生成結果に表示を合わせる
  useEffect(() => {
    setResults(initial ?? {});
    setError("");
  }, [jobId, initial]);

  if (!llm?.enabled || segments.length === 0) return null;

  /** kind (summary / minutes) の生成をサーバーに依頼する。数十秒〜数分かかる。 */
  async function generate(kind: string) {
    setError("");
    setLoading(kind);
    try {
      const res = jobId
        ? await fetch(`/api/jobs/${jobId}/summaries`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind }),
          })
        : await fetch("/api/summarize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind, segments, speaker_names: names }),
          });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.detail ?? `生成に失敗しました (${res.status})`);
        return;
      }
      const result: SummaryResult = await res.json();
      setResults((prev) => ({ ...prev, [result.kind]: result }));
    } catch {
      setError("生成リクエストに失敗しました");
    } finally {
      setLoading(null);
    }
  }

  return (
    <details className="panel" open>
      <summary>
        要約・議事録
        <span className="summary-note">ローカル LLM({llm.model})で生成</span>
      </summary>
      <div className="panel-body">
        <div className="toolbar">
          {KIND_LABELS.map(([kind, label]) => (
            <button
              key={kind}
              className="small"
              disabled={loading !== null}
              onClick={() => generate(kind)}
            >
              {loading === kind
                ? "生成中…"
                : results[kind]
                  ? `${label}を再生成`
                  : `${label}を生成`}
            </button>
          ))}
          {loading && (
            <span className="status-note">
              ローカル LLM で生成しています。1〜2分かかることがあります…
            </span>
          )}
        </div>
        {error && <p className="error">{error}</p>}
        {KIND_LABELS.map(([kind, label]) => {
          const r = results[kind];
          if (!r) return null;
          return (
            <div key={kind} className="summary-result">
              <div className="toolbar">
                <strong>{label}</strong>
                <span className="spacer" />
                <button
                  className="small"
                  onClick={() =>
                    downloadText(`${filenameBase}-${label}.md`, r.content + "\n")
                  }
                >
                  ダウンロード (MD)
                </button>
              </div>
              <pre className="summary-content">{r.content}</pre>
            </div>
          );
        })}
      </div>
    </details>
  );
}

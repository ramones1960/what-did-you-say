/**
 * 完了したジョブの文字起こしに頻出する単語を出現回数つきで一覧するパネル。
 *
 * 目的: 認識が怪しい語(人名・製品名・専門用語など)を見つけ、「用語に追加」で
 * 用語リストへ足してから同じファイルを再アップロードすると認識精度が上がる。
 * 集計はサーバー(GET /api/jobs/{id}/words)が行う(仕様は server/wordfreq.py)。
 */
import { useCallback, useRef, useState } from "react";

interface WordCount {
  word: string;
  count: number;
}

/** 用語リスト(改行・カンマ区切り)を語の集合に分解する。追加済み判定に使う。 */
function vocabularyTerms(vocabulary: string): Set<string> {
  return new Set(
    vocabulary
      .split(/[\n,、]/)
      .map((t) => t.trim())
      .filter(Boolean),
  );
}

export default function WordFrequencies({
  jobId,
  vocabulary,
  onAddWord,
}: {
  jobId: string;
  vocabulary: string;
  onAddWord: (word: string) => void;
}) {
  const [words, setWords] = useState<WordCount[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const loadedFor = useRef("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`/api/jobs/${jobId}/words?min_count=2&limit=100`);
      if (!res.ok) {
        setError("頻出単語の集計に失敗しました");
        return;
      }
      const body: { words: WordCount[] } = await res.json();
      setWords(body.words);
      setLoaded(true);
      loadedFor.current = jobId;
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  // パネルを開いた最初の1回だけ集計する(別ジョブを開いたら取り直す)
  function onToggle(e: React.SyntheticEvent<HTMLDetailsElement>) {
    if (e.currentTarget.open && !loading && loadedFor.current !== jobId) load();
  }

  const inVocab = vocabularyTerms(vocabulary);

  return (
    <details className="panel" onToggle={onToggle}>
      <summary>
        頻出単語
        <span className="summary-note">認識が怪しい語を用語リストに追加できます</span>
      </summary>
      <div className="panel-body">
        <p className="hint">
          文字起こしに繰り返し出てきた語(漢字語・カタカナ語・英数字語)を多い順に表示します。誤認識されている語を「用語に追加」で用語リストへ足し、同じファイルをもう一度アップロードすると認識精度が上がりやすくなります。
        </p>
        {loading && <p className="status-note">集計中…</p>}
        {error && <p className="error">{error}</p>}
        {loaded && !loading && words.length === 0 && (
          <p className="empty-state">繰り返し出てくる単語は見つかりませんでした。</p>
        )}
        {words.length > 0 && (
          <ul className="wordfreq-list">
            {words.map((w) => {
              const added = inVocab.has(w.word);
              return (
                <li key={w.word} className="wordfreq-item">
                  <span className="wordfreq-word">{w.word}</span>
                  <span className="badge">{w.count}回</span>
                  <button
                    className="small"
                    disabled={added}
                    onClick={() => onAddWord(w.word)}
                  >
                    {added ? "追加済み" : "用語に追加"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </details>
  );
}

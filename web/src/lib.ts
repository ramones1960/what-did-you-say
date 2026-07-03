/**
 * 画面間で共有する型定義とユーティリティ。
 * サーバー側の対応: Segment/Job は server/db.py の segments/jobs テーブル、
 * speakerLabel の仕様は server/exporters.py の speaker_label と揃えている。
 */

export interface Segment {
  start: number;
  end: number;
  text: string;
  speaker?: number | null;
}

export type SpeakerNames = Record<string, string>;

/** 話者番号を表示名に変換する(氏名未設定なら「話者N」の仮名)。 */
export function speakerLabel(
  speaker: number | null | undefined,
  names: SpeakerNames,
): string | null {
  if (speaker == null) return null;
  const name = names[String(speaker)]?.trim();
  return name || `話者${speaker}`;
}

/** セグメント一覧に登場する話者番号(昇順・重複なし)。 */
export function uniqueSpeakers(segments: Segment[]): number[] {
  const set = new Set<number>();
  for (const s of segments) if (s.speaker != null) set.add(s.speaker);
  return [...set].sort((a, b) => a - b);
}

export interface Job {
  id: string;
  filename: string;
  status: "queued" | "processing" | "done" | "error";
  error: string | null;
  language: string | null;
  duration: number | null;
  progress: number;
  created_at: number;
  speaker_names?: string | null;
  segments?: Segment[];
}

export function hms(seconds: number): string {
  const s = Math.floor(seconds);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

export function wsUrl(path: string): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}${path}`;
}

export function downloadText(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

export const LANGUAGES: [string, string][] = [
  ["", "自動判定"],
  ["ja", "日本語"],
  ["en", "英語"],
  ["zh", "中国語"],
  ["ko", "韓国語"],
];

// --- 発言の区切り(セグメント結合) ---
// 認識は細かい粒度のまま保存し、表示・TXT出力時に結合する。
// ルールとパラメータはサーバー側 (server/exporters.py) と揃えること。

export type Granularity = "short" | "standard" | "long";

export const GRANULARITIES: [Granularity, string][] = [
  ["short", "短い"],
  ["standard", "標準"],
  ["long", "長い"],
];

/** 結合パラメータ: [結合する無音間隔(秒), 結合後の最大長(秒), 最大文字数] */
const MERGE_PARAMS: Record<Granularity, [number, number, number] | null> = {
  short: null, // 結合しない(認識されたままの粒度)
  standard: [1.5, 30, 120],
  long: [4.0, 60, 240],
};

/**
 * テキストを連結する。欧文どうし(前が ASCII で終わり、次が英数字で始まる)
 * のときだけ空白を挟む。日本語どうしは空白なしで繋がる。
 */
function joinText(a: string, b: string): string {
  const needSpace =
    a.length > 0 &&
    a.charCodeAt(a.length - 1) < 128 &&
    a[a.length - 1] !== " " &&
    /^[A-Za-z0-9]/.test(b);
  return a + (needSpace ? " " : "") + b;
}

/**
 * 連続セグメントを「同一話者・間隔が gap 未満・上限以内」の条件で結合する。
 * 時刻タグは結合ブロック先頭の時刻になる。
 */
export function mergeSegments(segments: Segment[], level: Granularity): Segment[] {
  const params = MERGE_PARAMS[level];
  if (!params || segments.length === 0) return segments;
  const [gap, maxDur, maxChars] = params;

  const merged: Segment[] = [];
  for (const seg of segments) {
    const last = merged[merged.length - 1];
    if (
      last &&
      (seg.speaker ?? null) === (last.speaker ?? null) &&
      seg.start - last.end < gap &&
      seg.end - last.start <= maxDur &&
      last.text.length + seg.text.length <= maxChars
    ) {
      last.end = seg.end;
      last.text = joinText(last.text, seg.text);
    } else {
      merged.push({ ...seg });
    }
  }
  return merged;
}

/** 発言の区切り設定を localStorage に永続化して読み出す。 */
export function loadGranularity(): Granularity {
  const v = localStorage.getItem("wdys.granularity");
  return v === "short" || v === "long" ? v : "standard";
}

export function saveGranularity(v: Granularity): void {
  localStorage.setItem("wdys.granularity", v);
}

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

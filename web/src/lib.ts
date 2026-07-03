export interface Segment {
  start: number;
  end: number;
  text: string;
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

/**
 * アプリのルート。ヘッダー・タブ・フッターのレイアウトを持つ。
 *
 * タブは「リアルタイム文字起こし (Recorder)」と「ファイルから文字起こし
 * (Upload)」の2画面。切替でアンマウントすると文字起こし結果や録音状態が
 * 消えるため、両方をマウントしたまま hidden 属性で表示だけ切り替える。
 */
import { useEffect, useState } from "react";
import Recorder from "./Recorder";
import Upload from "./Upload";

export default function App() {
  const [tab, setTab] = useState<"realtime" | "file">("realtime");
  const [model, setModel] = useState("");

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then((h) => setModel(h.model ?? ""))
      .catch(() => {});
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-title">
          <span className="app-mark" aria-hidden>
            音
          </span>
          <div>
            <h1>What did you say?</h1>
            <p>文字起こしツール</p>
          </div>
        </div>
        <div className="app-meta">
          <span className="badge badge-secure" title="音声・テキストは外部のサービスに送信されません">
            ローカル処理
          </span>
          {model && <span className="badge">認識モデル: {model}</span>}
        </div>
      </header>

      <main>
        <nav className="tabs" role="tablist">
          <button
            role="tab"
            aria-selected={tab === "realtime"}
            className={tab === "realtime" ? "active" : ""}
            onClick={() => setTab("realtime")}
          >
            リアルタイム文字起こし
          </button>
          <button
            role="tab"
            aria-selected={tab === "file"}
            className={tab === "file" ? "active" : ""}
            onClick={() => setTab("file")}
          >
            ファイルから文字起こし
          </button>
        </nav>

        {/* タブ切替でアンマウントすると文字起こし結果や録音状態が消えるため、
            両方マウントしたまま表示だけ切り替える */}
        <div hidden={tab !== "realtime"}>
          <p className="tab-lead">
            マイクの音声を発話の区切りごとにテキスト化します。結果には時刻と話者のタグが付き、停止後にテキストファイルとして保存できます。
          </p>
          <Recorder />
        </div>
        <div hidden={tab !== "file"}>
          <p className="tab-lead">
            音声・動画ファイルをアップロードして文字起こしします。処理はサーバー内で順番に実行され、結果は
            TXT / SRT / VTT / JSON 形式でダウンロードできます。
          </p>
          <Upload />
        </div>
      </main>

      <footer className="app-footer">
        すべての処理はこのサーバー内で完結します。音声データ・文字起こし結果が外部に送信されることはありません。
      </footer>
    </div>
  );
}

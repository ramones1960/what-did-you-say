import { useState } from "react";
import Recorder from "./Recorder";
import Upload from "./Upload";

export default function App() {
  const [tab, setTab] = useState<"realtime" | "file">("realtime");

  return (
    <main>
      <header>
        <h1>what did you say?</h1>
        <p className="muted">完全ローカルで動く文字起こし — 音声はどこにも送信されません</p>
      </header>
      <nav className="tabs">
        <button
          className={tab === "realtime" ? "active" : ""}
          onClick={() => setTab("realtime")}
        >
          🎙 リアルタイム
        </button>
        <button
          className={tab === "file" ? "active" : ""}
          onClick={() => setTab("file")}
        >
          📁 ファイル
        </button>
      </nav>
      {/* タブ切替でアンマウントすると文字起こし結果や録音状態が消えるため、
          両方マウントしたまま表示だけ切り替える */}
      <div hidden={tab !== "realtime"}>
        <Recorder />
      </div>
      <div hidden={tab !== "file"}>
        <Upload />
      </div>
    </main>
  );
}

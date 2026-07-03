import { useCallback, useEffect, useState } from "react";

const MAX_CHARS = 1000;
const SYNC_EVENT = "wdys-prompt-changed";
const PRESETS_EVENT = "wdys-presets-changed";

export interface PromptValues {
  vocabulary: string;
  context: string;
}

interface Preset extends PromptValues {
  id: string;
  name: string;
}

function read(): PromptValues {
  return {
    vocabulary: localStorage.getItem("wdys.vocabulary") ?? "",
    context: localStorage.getItem("wdys.context") ?? "",
  };
}

/** 用語リスト・コンテキストを localStorage に永続化し、タブ間(両画面)で同期する。 */
export function usePromptSettings(): [PromptValues, (v: PromptValues) => void] {
  const [values, setValues] = useState<PromptValues>(read);

  useEffect(() => {
    const sync = () => setValues(read());
    window.addEventListener(SYNC_EVENT, sync);
    return () => window.removeEventListener(SYNC_EVENT, sync);
  }, []);

  const update = useCallback((v: PromptValues) => {
    localStorage.setItem("wdys.vocabulary", v.vocabulary);
    localStorage.setItem("wdys.context", v.context);
    setValues(v);
    window.dispatchEvent(new Event(SYNC_EVENT));
  }, []);

  return [values, update];
}

export default function PromptSettings({
  values,
  onChange,
  disabled,
}: {
  values: PromptValues;
  onChange: (v: PromptValues) => void;
  disabled?: boolean;
}) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [presetName, setPresetName] = useState("");
  const [message, setMessage] = useState("");
  // 開閉は初期表示時のみ内容の有無で決め、以後はユーザー操作に任せる
  const [initialOpen] = useState(
    () => !!(values.vocabulary.trim() || values.context.trim()),
  );

  const fetchPresets = useCallback(async () => {
    const res = await fetch("/api/presets");
    if (res.ok) setPresets(await res.json());
  }, []);

  // 初回取得 + もう一方のタブでの保存・削除に追従する
  useEffect(() => {
    fetchPresets();
    window.addEventListener(PRESETS_EVENT, fetchPresets);
    return () => window.removeEventListener(PRESETS_EVENT, fetchPresets);
  }, [fetchPresets]);

  function applyPreset(id: string) {
    setSelectedId(id);
    setMessage("");
    const preset = presets.find((p) => p.id === id);
    if (preset) {
      onChange({ vocabulary: preset.vocabulary, context: preset.context });
      setPresetName(preset.name);
    }
  }

  async function savePreset() {
    const name = presetName.trim();
    if (!name) {
      setMessage("プリセット名を入力してください");
      return;
    }
    const res = await fetch("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, ...values }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      setMessage(body?.detail ?? "保存に失敗しました");
      return;
    }
    const saved: Preset = await res.json();
    setSelectedId(saved.id);
    setMessage(`「${saved.name}」を保存しました`);
    await fetchPresets();
    window.dispatchEvent(new Event(PRESETS_EVENT));
  }

  async function deletePreset() {
    const preset = presets.find((p) => p.id === selectedId);
    if (!preset) return;
    if (!window.confirm(`プリセット「${preset.name}」を削除しますか?`)) return;
    const res = await fetch(`/api/presets/${preset.id}`, { method: "DELETE" });
    if (res.ok) {
      setSelectedId("");
      setPresetName("");
      setMessage(`「${preset.name}」を削除しました`);
      await fetchPresets();
      window.dispatchEvent(new Event(PRESETS_EVENT));
    }
  }

  const hasContent = values.vocabulary.trim() || values.context.trim();
  return (
    <details className="panel" open={initialOpen}>
      <summary>
        認識精度の設定
        <span className="summary-note">用語リスト・コンテキスト(任意)</span>
        {hasContent ? <span className="badge badge-accent">設定あり</span> : null}
      </summary>
      <div className="panel-body">
        <div className="preset-bar">
          <select
            value={selectedId}
            onChange={(e) => applyPreset(e.target.value)}
            disabled={disabled}
            aria-label="プリセットを選択"
          >
            <option value="">プリセットを選択…</option>
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <input
            type="text"
            placeholder="プリセット名(例: 開発定例)"
            maxLength={100}
            value={presetName}
            disabled={disabled}
            onChange={(e) => setPresetName(e.target.value)}
          />
          <button className="small" onClick={savePreset} disabled={disabled}>
            現在の内容をプリセット保存
          </button>
          {selectedId && (
            <button className="small" onClick={deletePreset} disabled={disabled}>
              削除
            </button>
          )}
          {message && <span className="status-note">{message}</span>}
        </div>
        <p className="hint">
          よく使う会議・案件ごとに用語リストとコンテキストをプリセットとして登録できます。プリセットはサーバーに保存され、他の利用者とも共有されます。同名で保存すると上書きされます。
        </p>
        <div className="prompt-fields">
          <label>
            <span className="field-label">用語リスト</span>
            <textarea
              rows={4}
              placeholder={"例:\n山田太郎\nDX推進室, 基幹システム刷新"}
              value={values.vocabulary}
              maxLength={MAX_CHARS}
              disabled={disabled}
              onChange={(e) => onChange({ ...values, vocabulary: e.target.value })}
            />
            <span className="hint">
              人名・製品名・部署名などを1行1語(またはカンマ区切り)で登録すると、正しく認識されやすくなります。目安は50語まで。
            </span>
          </label>
          <label>
            <span className="field-label">コンテキスト</span>
            <textarea
              rows={4}
              placeholder={"例: 製品開発部の週次定例会議。議題はリリース計画とバグ対応の優先順位。"}
              value={values.context}
              maxLength={MAX_CHARS}
              disabled={disabled}
              onChange={(e) => onChange({ ...values, context: e.target.value })}
            />
            <span className="hint">
              会議や音声の概要を1〜2文で入力すると、文脈に沿った表記になりやすくなります。
            </span>
          </label>
        </div>
      </div>
    </details>
  );
}

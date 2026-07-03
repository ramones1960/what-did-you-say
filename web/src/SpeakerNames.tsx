import { SpeakerNames as Names } from "./lib";

/** 検出された話者(仮名)に氏名を入力して一括で振り分けるパネル。 */
export default function SpeakerNamesPanel({
  speakers,
  names,
  onChange,
  onSave,
  saving,
}: {
  speakers: number[];
  names: Names;
  onChange: (names: Names) => void;
  onSave?: () => void;
  saving?: boolean;
}) {
  if (speakers.length === 0) return null;
  return (
    <details className="prompt-settings" open>
      <summary>話者の氏名({speakers.length}名を検出)</summary>
      <div className="speaker-names">
        {speakers.map((n) => (
          <label key={n}>
            <span className={`spk spk-c${(n - 1) % 6}`}>話者{n}</span>
            <input
              type="text"
              placeholder="氏名を入力"
              maxLength={100}
              value={names[String(n)] ?? ""}
              onChange={(e) => onChange({ ...names, [String(n)]: e.target.value })}
            />
          </label>
        ))}
        {onSave && (
          <button className="small" onClick={onSave} disabled={saving}>
            {saving ? "保存中…" : "保存(エクスポートに反映)"}
          </button>
        )}
      </div>
      <p className="hint">入力すると表示中の「話者N」がすべて置き換わります。</p>
    </details>
  );
}

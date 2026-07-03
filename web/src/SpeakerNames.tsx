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
    <details className="panel" open>
      <summary>
        話者の氏名設定
        <span className="summary-note">{speakers.length}名を検出</span>
      </summary>
      <div className="panel-body">
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
              {saving ? "保存中…" : "保存してエクスポートに反映"}
            </button>
          )}
        </div>
        <p className="hint">
          氏名を入力すると、該当する話者の発言すべてに一括で反映されます。空欄の話者は「話者N」のまま表示されます。
        </p>
      </div>
    </details>
  );
}

import { useCallback, useEffect, useState } from "react";

const MAX_CHARS = 1000;
const SYNC_EVENT = "wdys-prompt-changed";

export interface PromptValues {
  vocabulary: string;
  context: string;
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
  const hasContent = values.vocabulary.trim() || values.context.trim();
  return (
    <details className="panel" open={!!hasContent}>
      <summary>
        認識精度の設定
        <span className="summary-note">用語リスト・コンテキスト(任意)</span>
        {hasContent ? <span className="badge badge-accent">設定あり</span> : null}
      </summary>
      <div className="panel-body prompt-fields">
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
            人名・製品名・部署名などを1行1語(またはカンマ区切り)で登録すると、正しく認識されやすくなります。目安は50語まで。入力内容はこのブラウザに保存され、次回も引き継がれます。
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
    </details>
  );
}

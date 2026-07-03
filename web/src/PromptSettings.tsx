import { useEffect, useState } from "react";

const MAX_CHARS = 1000;

export interface PromptValues {
  vocabulary: string;
  context: string;
}

/** 用語リスト・コンテキストを localStorage に永続化して保持する。 */
export function usePromptSettings(): [PromptValues, (v: PromptValues) => void] {
  const [values, setValues] = useState<PromptValues>(() => ({
    vocabulary: localStorage.getItem("wdys.vocabulary") ?? "",
    context: localStorage.getItem("wdys.context") ?? "",
  }));
  useEffect(() => {
    localStorage.setItem("wdys.vocabulary", values.vocabulary);
    localStorage.setItem("wdys.context", values.context);
  }, [values]);
  return [values, setValues];
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
    <details className="prompt-settings" open={!!hasContent}>
      <summary>
        精度向上の設定(用語リスト・コンテキスト)
        {hasContent ? <span className="badge">設定中</span> : null}
      </summary>
      <div className="prompt-fields">
        <label>
          用語リスト
          <textarea
            rows={4}
            placeholder={"固有名詞・専門用語を1行1語またはカンマ区切りで\n例:\n山田太郎\nDX推進室, SaaS"}
            value={values.vocabulary}
            maxLength={MAX_CHARS}
            disabled={disabled}
            onChange={(e) => onChange({ ...values, vocabulary: e.target.value })}
          />
          <span className="hint">
            認識されやすくなります(数十語程度まで。多すぎると効果が薄れます)
          </span>
        </label>
        <label>
          コンテキスト
          <textarea
            rows={4}
            placeholder={"音声の前提を文章で\n例: これは製品開発チームの週次定例会議です。議題はリリース計画とバグ対応の優先順位です。"}
            value={values.context}
            maxLength={MAX_CHARS}
            disabled={disabled}
            onChange={(e) => onChange({ ...values, context: e.target.value })}
          />
          <span className="hint">話題・文体のヒントとして冒頭の認識に反映されます</span>
        </label>
      </div>
    </details>
  );
}

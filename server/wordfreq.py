"""文字起こし結果から頻出単語を数える(用語リスト調整の支援)。

目的: 音声中で繰り返し出てくる語を出現回数つきで洗い出し、認識が怪しい語を
用語リスト(hotwords)に足して再実行してもらうためのヒントを提供する。

日本語は分かち書きされないため形態素解析が必要になるが、外部辞書や重い依存を
足さない方針に合わせて、正規表現で「用語として足す価値がありそうなトークン」だけを
抽出する軽量方式にしている:

  - 漢字語(2文字以上の漢字連続): 固有名詞・専門用語の多くを拾える
  - カタカナ語(2文字以上): 外来語・製品名など認識が揺れやすい語の中心
  - 英数字語(アルファベット始まり、2文字以上): 略語・製品名・型番など

ひらがな主体の語や助詞などの機能語は用語リストの対象になりにくいため拾わない。
完全な分かち書きではないので長い複合語は分割されないが、「同じ表記の繰り返しを
見つける」用途には十分で、完全ローカル・無依存を保てる。
"""

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

# 抽出対象トークン(いずれか)。順序は関係ない(findall は最長一致で左から拾う)。
_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]+"        # 英数字語(アルファベット始まり・2文字以上)
    r"|[ァ-ヶ][ァ-ヶー・]+"  # カタカナ語(2文字以上)
    r"|[一-鿿々]{2,}"  # 漢字語(2文字以上)
)


def count_words(
    texts: Iterable[str], min_count: int = 2, limit: int = 100
) -> list[dict[str, Any]]:
    """テキスト群から頻出トークンを数え、多い順に返す。

    min_count 未満の語は除外し、上位 limit 件までを返す。返り値は
    [{"word": 単語, "count": 出現回数}, ...] で count の降順(同数は初出順)。
    """
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(_TOKEN_RE.findall(text))
    return [
        {"word": word, "count": count}
        for word, count in counter.most_common()
        if count >= min_count
    ][:limit]

"""頻出単語カウント(wordfreq.count_words)のテスト。"""

from server import wordfreq


class TestCountWords:
    def test_出現回数の多い順に返す(self):
        texts = ["基幹システムの刷新", "基幹システムを検討", "基幹システムの予算"]
        words = wordfreq.count_words(texts, min_count=1)
        counts = {w["word"]: w["count"] for w in words}
        assert counts["基幹"] == 3
        assert counts["システム"] == 3
        # 降順であること
        assert words[0]["count"] >= words[-1]["count"]

    def test_min_count未満は除外する(self):
        texts = ["リリース計画", "リリース日程", "バグ対応"]
        words = wordfreq.count_words(texts, min_count=2)
        got = {w["word"] for w in words}
        assert "リリース" in got  # 2回
        assert "バグ" not in got  # 1回

    def test_カタカナ_漢字_英数字を拾いひらがなや記号は拾わない(self):
        texts = ["APIをテストする", "APIをテストする"]
        got = {w["word"] for w in wordfreq.count_words(texts, min_count=1)}
        assert "API" in got       # 英数字語
        assert "テスト" in got    # カタカナ語
        assert "を" not in got     # ひらがな(機能語)は対象外
        assert "する" not in got

    def test_一文字の語は拾わない(self):
        texts = ["部の会議", "課の会議"]
        got = {w["word"] for w in wordfreq.count_words(texts, min_count=1)}
        assert "会議" in got  # 2文字の漢字語
        assert "部" not in got  # 1文字は対象外
        assert "課" not in got

    def test_カタカナの長音を含む語を1語として数える(self):
        texts = ["サーバーの再起動", "サーバーの設定"]
        got = {w["word"]: w["count"] for w in wordfreq.count_words(texts, min_count=1)}
        assert got.get("サーバー") == 2

    def test_limitで上位のみ返す(self):
        texts = ["会議 予算 計画 課題 議事録"] * 3
        words = wordfreq.count_words(texts, min_count=1, limit=2)
        assert len(words) == 2

    def test_空入力(self):
        assert wordfreq.count_words([], min_count=1) == []

"""exporters.py のセグメント結合・整形ロジックのテスト。

結合パラメータの正典 shared/merge_params.json が正しく読めていることと、
結合ルール(同一話者・間隔・上限)が仕様どおりであることを確認する。
"""

import json
from pathlib import Path

from server import exporters


def seg(start, end, text, speaker=None):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


class TestMergeParams:
    def test_shared_jsonから読み込まれる(self):
        shared = json.loads(
            (Path(__file__).parent.parent / "shared" / "merge_params.json").read_text(
                encoding="utf-8"
            )
        )
        assert set(exporters.MERGE_PARAMS) == set(shared)
        assert exporters.MERGE_PARAMS["short"] is None
        p = shared["standard"]
        assert exporters.MERGE_PARAMS["standard"] == (
            p["gap"],
            p["max_duration"],
            p["max_chars"],
        )


class TestMergeSegments:
    def test_shortは結合しない(self):
        segments = [seg(0, 1, "あ"), seg(1.1, 2, "い")]
        assert exporters.merge_segments(segments, "short") == segments

    def test_未知のgranularityは結合しない(self):
        segments = [seg(0, 1, "あ"), seg(1.1, 2, "い")]
        assert exporters.merge_segments(segments, "unknown") == segments

    def test_同一話者かつ間隔が近ければ結合される(self):
        merged = exporters.merge_segments(
            [seg(0, 1, "こんにちは", 1), seg(1.5, 2.5, "世界", 1)], "standard"
        )
        assert len(merged) == 1
        assert merged[0]["text"] == "こんにちは世界"
        assert merged[0]["start"] == 0
        assert merged[0]["end"] == 2.5

    def test_話者が違うと結合されない(self):
        merged = exporters.merge_segments(
            [seg(0, 1, "あ", 1), seg(1.2, 2, "い", 2)], "standard"
        )
        assert len(merged) == 2

    def test_間隔が開くと結合されない(self):
        # standard の gap は 1.5 秒
        merged = exporters.merge_segments(
            [seg(0, 1, "あ", 1), seg(3.0, 4, "い", 1)], "standard"
        )
        assert len(merged) == 2

    def test_最大文字数を超えると結合されない(self):
        long_text = "あ" * 100
        merged = exporters.merge_segments(
            [seg(0, 1, long_text, 1), seg(1.2, 2, long_text, 1)], "standard"
        )
        assert len(merged) == 2

    def test_最大長を超えると結合されない(self):
        # standard の最大長は 30 秒
        merged = exporters.merge_segments(
            [seg(0, 20, "あ", 1), seg(21, 45, "い", 1)], "standard"
        )
        assert len(merged) == 2

    def test_元のリストは破壊されない(self):
        segments = [seg(0, 1, "あ", 1), seg(1.2, 2, "い", 1)]
        exporters.merge_segments(segments, "standard")
        assert segments[0]["text"] == "あ"

    def test_欧文は空白を挟んで結合される(self):
        merged = exporters.merge_segments(
            [seg(0, 1, "Hello", 1), seg(1.2, 2, "world", 1)], "standard"
        )
        assert merged[0]["text"] == "Hello world"


class TestFormats:
    def test_txt(self):
        out = exporters.to_txt([seg(0, 1, "こんにちは", 1)])
        assert out == "[00:00:00] 話者1: こんにちは\n"

    def test_txtで氏名が反映される(self):
        out = exporters.to_txt([seg(0, 1, "こんにちは", 1)], {"1": "山田"})
        assert out == "[00:00:00] 山田: こんにちは\n"

    def test_話者なしはラベルなし(self):
        out = exporters.to_txt([seg(3661, 3662, "こんにちは")])
        assert out == "[01:01:01] こんにちは\n"

    def test_srt(self):
        out = exporters.to_srt([seg(0, 1.5, "こんにちは")])
        assert "1\n00:00:00,000 --> 00:00:01,500\nこんにちは\n" in out

    def test_vtt(self):
        out = exporters.to_vtt([seg(0, 1.5, "こんにちは")])
        assert out.startswith("WEBVTT\n\n")
        assert "00:00:00.000 --> 00:00:01.500\nこんにちは\n" in out


class TestSpeakerLabel:
    def test_仮名(self):
        assert exporters.speaker_label(2, None) == "話者2"

    def test_氏名(self):
        assert exporters.speaker_label(2, {"2": " 佐藤 "}) == "佐藤"

    def test_空の氏名は仮名のまま(self):
        assert exporters.speaker_label(2, {"2": "  "}) == "話者2"

    def test_話者なし(self):
        assert exporters.speaker_label(None, {"1": "山田"}) is None

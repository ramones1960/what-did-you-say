"""diarize.map_speakers(一括話者分離の結果をセグメントへ割り当てる)のテスト。

モデルを使う部分(埋め込み抽出・一括話者分離の推論)はモデルダウンロードが
必要なため対象外(conftest.py で DIARIZATION=0 に設定済み)。
"""

from server import diarize


def seg(idx: int, start: float, end: float) -> dict:
    return {"idx": idx, "start": start, "end": end, "text": "", "speaker": None}


class TestMapSpeakers:
    def test_重なりが最大の話者区間に割り当てる(self):
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        mapping = diarize.map_speakers(
            [seg(0, 0, 2), seg(1, 4, 9), seg(2, 6, 8)], turns
        )
        # seg1 は両区間にまたがるが、重なりが大きい後半の話者になる
        assert mapping == {0: 1, 1: 2, 2: 2}

    def test_話者番号は登場順に1から振り直される(self):
        # 生のクラスタ番号(7, 2)がそのまま出ないこと
        turns = [(0.0, 3.0, 7), (3.0, 6.0, 2)]
        mapping = diarize.map_speakers([seg(0, 0, 1), seg(1, 4, 5)], turns)
        assert mapping == {0: 1, 1: 2}

    def test_重ならないセグメントは最も近い区間に寄せる(self):
        turns = [(0.0, 2.0, 0), (10.0, 12.0, 1)]
        mapping = diarize.map_speakers([seg(0, 3.0, 4.0)], turns)
        assert mapping == {0: 1}  # 前の区間(距離1秒)の方が近い

    def test_話者区間が空なら空辞書(self):
        # 呼び出し側が既存(逐次)の割り当てを維持できるように空を返す
        assert diarize.map_speakers([seg(0, 0, 1)], []) == {}

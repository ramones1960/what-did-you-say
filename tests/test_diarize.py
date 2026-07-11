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


class _FakeSegment:
    def __init__(self, start: float, end: float, speaker: int):
        self.start, self.end, self.speaker = start, end, speaker


class _FakeResult:
    def __init__(self, segments: list[_FakeSegment]):
        self._segments = segments

    def sort_by_start_time(self):
        return self._segments


class _FakeDiarizer:
    """set_config に渡ったクラスタ数を記録し、用意した結果を順に返すダミー。"""

    def __init__(self, results: list[_FakeResult]):
        self._results = list(results)
        self.cluster_history: list[int | None] = []

    def set_config(self, num_clusters):
        # テストでは _diarizer_config がクラスタ数をそのまま返すよう差し替える
        self.cluster_history.append(num_clusters)

    def process(self, audio, callback=None):
        return self._results.pop(0)


def _speakers(n: int) -> _FakeResult:
    """n 人分の話者区間(1人1区間)を持つ結果を作る。"""
    return _FakeResult([_FakeSegment(i, i + 1, i) for i in range(n)])


class TestDiarizeOfflineRange:
    """話者の人数の幅指定 → 自動推定が範囲外のときだけ再実行するロジック。

    モデルは使わず、_get_diarizer / _diarizer_config を差し替えて検証する。
    """

    def _patch(self, monkeypatch, fake: _FakeDiarizer):
        monkeypatch.setattr(diarize, "_get_diarizer", lambda: fake)
        monkeypatch.setattr(diarize, "_diarizer_config", lambda n: n)

    def test_範囲内なら1回で終わる(self, monkeypatch):
        fake = _FakeDiarizer([_speakers(3)])
        self._patch(monkeypatch, fake)
        turns = diarize.diarize_offline(None, min_speakers=2, max_speakers=5)
        assert fake.cluster_history == [None]  # 自動推定のみ
        assert len({spk for _, _, spk in turns}) == 3

    def test_最大を超えたら最大に固定してやり直す(self, monkeypatch):
        fake = _FakeDiarizer([_speakers(7), _speakers(4)])
        self._patch(monkeypatch, fake)
        turns = diarize.diarize_offline(None, max_speakers=4)
        assert fake.cluster_history == [None, 4]
        assert len({spk for _, _, spk in turns}) == 4

    def test_最小を下回ったら最小に固定してやり直す(self, monkeypatch):
        fake = _FakeDiarizer([_speakers(1), _speakers(3)])
        self._patch(monkeypatch, fake)
        turns = diarize.diarize_offline(None, min_speakers=3)
        assert fake.cluster_history == [None, 3]
        assert len({spk for _, _, spk in turns}) == 3

    def test_同数指定はクラスタ数固定で1回(self, monkeypatch):
        fake = _FakeDiarizer([_speakers(2)])
        self._patch(monkeypatch, fake)
        diarize.diarize_offline(None, min_speakers=2, max_speakers=2)
        assert fake.cluster_history == [2]

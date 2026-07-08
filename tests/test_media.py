"""media.py の ffmpeg エラー分類のテスト(ffmpeg 実行はモックする)。"""

import subprocess

import pytest

from server import media


def _fake_run(stderr: str):
    """returncode=1 で指定 stderr を返す subprocess.run の代役。"""

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)

    return run


class TestExplainFfmpegError:
    def test_moovエラーはOBS向けの案内になる(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            media.subprocess,
            "run",
            _fake_run("[mov,mp4,...] moov atom not found\n"),
        )
        with pytest.raises(media.MediaError) as e:
            media.to_wav16k(tmp_path / "src.mp4", tmp_path / "dst.wav")
        msg = str(e.value)
        assert "moov atom" in msg
        assert "OBS" in msg
        assert "MKV" in msg

    def test_未知のエラーは原文を通す(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            media.subprocess,
            "run",
            _fake_run("Invalid data found when processing input"),
        )
        with pytest.raises(media.MediaError) as e:
            media.to_wav16k(tmp_path / "src.mp4", tmp_path / "dst.wav")
        msg = str(e.value)
        assert "音声の抽出に失敗しました" in msg
        assert "Invalid data found" in msg

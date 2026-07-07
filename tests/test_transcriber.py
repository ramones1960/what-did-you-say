"""transcriber.explain_inference_error のテスト(GPU 系エラーの文言変換)。"""

from server import transcriber


class TestExplainInferenceError:
    def test_VRAM不足は対処法つきの文言になる(self):
        e = RuntimeError("CUDA failed with error out of memory")
        msg = transcriber.explain_inference_error(e)
        assert "VRAM" in msg
        assert "int8_float16" in msg
        assert "out of memory" in msg  # 原文も残す

    def test_cuDNN欠落は再ビルドを案内する(self):
        e = RuntimeError("Library libcudnn_ops.so.9 is not found or cannot be loaded")
        assert "再ビルド" in transcriber.explain_inference_error(e)

    def test_cuBLAS欠落も再ビルドを案内する(self):
        e = RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
        assert "再ビルド" in transcriber.explain_inference_error(e)

    def test_その他のCUDAエラーはCPU切替を案内する(self):
        e = RuntimeError("CUDA driver version is insufficient for CUDA runtime version")
        assert "WHISPER_DEVICE=cpu" in transcriber.explain_inference_error(e)

    def test_GPUと無関係なエラーは原文のまま(self):
        e = ValueError("なにか別の問題")
        assert transcriber.explain_inference_error(e) == "なにか別の問題"

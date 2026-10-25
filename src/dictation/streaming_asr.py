from __future__ import annotations

from pathlib import Path

MODEL_ID = "csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en"
MODEL_REVISION = "8e40c43232a1c5c66c82111efc5820d3accca11b"
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt")
PREVIEW_THREADS = 2


class StreamingASR:
    """A session-owned incremental stream, with endpoint detection disabled."""

    def __init__(self, directory: Path):
        import sherpa_onnx
        for name in MODEL_FILES:
            if not (directory / name).is_file():
                raise RuntimeError("流式模型未预下载，请运行 scripts/setup_dictation.py")
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
            tokens=str(directory / "tokens.txt"), encoder=str(directory / MODEL_FILES[0]),
            decoder=str(directory / MODEL_FILES[1]), num_threads=PREVIEW_THREADS, provider="cpu",
            enable_endpoint_detection=False,
        )
        self.reset()

    def reset(self):
        self.stream = self.recognizer.create_stream()
        self.text = ""

    def feed(self, samples, *, finish=False):
        self.stream.accept_waveform(16000, samples)
        if finish:
            # Only the recognizer receives padding; stored PCM/segment times stay real.
            import numpy as np
            self.stream.accept_waveform(16000, np.zeros(10560, dtype=np.float32))
            self.stream.input_finished()
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        self.text = self.recognizer.get_result(self.stream)
        return self.text

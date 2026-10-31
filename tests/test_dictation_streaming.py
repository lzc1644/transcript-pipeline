"""The preview thread budget is independent of the shared GPU worker."""
import importlib.metadata
import io
import sys
from types import SimpleNamespace

import pytest

from src.dictation.streaming_asr import MODEL_FILES, StreamingASR


def test_streaming_preview_requests_two_cpu_threads(tmp_path, monkeypatch):
    for name in MODEL_FILES:
        (tmp_path / name).touch()
    captured = {}
    recognizer = SimpleNamespace(create_stream=lambda: object())
    def create(**kwargs):
        captured.update(kwargs)
        return recognizer
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OnlineRecognizer=SimpleNamespace(from_paraformer=create)))
    asr = StreamingASR(tmp_path)
    assert captured["num_threads"] == 2
    assert captured["provider"] == "cpu"
    assert captured["enable_endpoint_detection"] is False
    assert asr.recognizer is recognizer


@pytest.mark.parametrize("preview", [True, False])
def test_cpu_ready_records_preview_thread_budget(monkeypatch, preview):
    import src.dictation.worker as worker
    import src.asr.streaming_audio as audio
    import src.asr.streaming as streaming
    monkeypatch.setattr(audio, "SileroVAD", lambda _: lambda _: 0)
    monkeypatch.setattr(streaming, "StreamingASR", lambda _: object())
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "test")
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO('{"op":"stop"}\n'))
    events = []
    monkeypatch.setattr(worker, "emit", events.append)
    worker.cpu_worker({"vad_model": "unused", "streaming_model": "unused", "segmentation": {}, "preview": preview})
    assert events[0]["metadata"]["preview_enabled"] is preview
    assert events[0]["metadata"]["preview_threads"] == (2 if preview else None)
    assert events[-1]["type"] == "drained"

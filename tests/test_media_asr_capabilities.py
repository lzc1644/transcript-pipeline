"""Standalone operations and their product adapters share the actual core."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from src.asr import file as file_asr
from src.asr.errors import AsrTranscriptionError
from src.asr.options import AsrResources, FileAsrOptions
from src.asr.results import AsrBatchItem, AsrFileResult, AsrSegmentResult
from src.asr.streaming import MODEL_FILES, StreamingASR
from src.asr.utterance import UtteranceASR
from src.config_loader import load_settings
from src.media import conversion
from tests.helpers import write_minimal_settings


def test_capability_imports_do_not_require_product_configuration_or_optional_models():
    script = '''
import importlib.abc
import sys
class NoProduct(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ("src.schemas", "src.config_loader", "src.dictation", "src.web", "src.job_runner",
                   "fastapi", "faster_whisper", "torch", "sherpa_onnx", "qwen_asr", "funasr")
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("capability imported " + fullname)
sys.meta_path.insert(0, NoProduct())
import src.media
import src.asr.file
import src.asr.results
import src.asr.streaming
import src.asr.streaming_audio
import src.asr.utterance
import src.pdf
'''
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_root_settings_export_keeps_original_function_object():
    from src import load_settings as exported
    assert exported is load_settings


def test_standalone_conversion_and_legacy_batch_use_same_ffmpeg_operation(tmp_path, monkeypatch):
    from src.ffmpeg_utils import extract_audio_batch
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    source = loaded.path_for("videos_dir") / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic container")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"normalized")
        return subprocess.CompletedProcess(command, 0, stderr="")

    monkeypatch.setattr(conversion.subprocess, "run", run)
    monkeypatch.setattr("src.ffmpeg_utils.find_ffmpeg_binary", lambda: "local-ffmpeg")
    results = extract_audio_batch(loaded)
    standalone = conversion.convert_audio(
        source, tmp_path / "standalone.flac", sample_rate=22050, channels=2,
        ffmpeg_binary="local-ffmpeg", overwrite=True)
    assert [call[0] for call in calls] == ["local-ffmpeg", "local-ffmpeg"]
    assert calls[0][-5:-1] == ["-ac", "1", "-ar", "16000"]
    assert calls[1][-5:-1] == ["-ac", "2", "-ar", "22050"]
    assert "-n" in calls[0] and "-y" in calls[1]
    assert standalone.audio_path.read_bytes() == results[0].audio_path.read_bytes() == b"normalized"
    assert source.read_bytes() == b"synthetic container"


def test_existing_conversion_output_skips_without_resolving_binary(tmp_path, monkeypatch):
    output = tmp_path / "existing.wav"
    output.write_bytes(b"existing")
    def unexpected():
        raise AssertionError("skipping existing audio must not need ffmpeg")
    monkeypatch.setattr(conversion, "find_ffmpeg_binary", unexpected)
    result = conversion.convert_audio(tmp_path / "source.mp4", output)
    assert result.status == "skipped" and output.read_bytes() == b"existing"


def whisper_stub(monkeypatch):
    calls = {"models": [], "transcriptions": [], "cache": []}
    class Model:
        def __init__(self, model_ref, **kwargs):
            calls["models"].append((model_ref, kwargs))
        def transcribe(self, path, **kwargs):
            calls["transcriptions"].append((path, kwargs))
            return iter([SimpleNamespace(id=7, start=.1, end=.9, text=" 固定边界结果。 ")]), SimpleNamespace(
                language="zh", duration=1.0, duration_after_vad=.8)
    def cached(model_size, cache):
        calls["cache"].append((model_size, cache))
        return None
    monkeypatch.setattr(file_asr, "import_whisper_model_class", lambda: Model)
    monkeypatch.setattr(file_asr, "resolve_cached_faster_whisper_model_path", cached)
    return calls


def test_file_batch_without_yaml_loads_once_and_publishes_compatible_results(tmp_path, monkeypatch):
    calls = whisper_stub(monkeypatch)
    audio = [tmp_path / name for name in ("first.wav", "second.wav")]
    for path in audio:
        path.write_bytes(b"synthetic audio fixture; model boundary is stubbed")
    options = FileAsrOptions("tiny", "cpu", "int8", beam_size=9, vad_filter=True,
                             condition_on_previous_text=False, initial_prompt="")
    resources = AsrResources(tmp_path / "cache", source_root=tmp_path, whisper_cache_subdir="custom-whisper")
    outputs = file_asr.transcribe_files(audio, tmp_path / "output", options=options, resources=resources)
    assert len(calls["models"]) == 1 and len(calls["transcriptions"]) == 2
    assert calls["cache"] == [("tiny", tmp_path / "cache/custom-whisper")]
    assert calls["models"][0] == ("tiny", {"device": "cpu", "compute_type": "int8",
                                            "download_root": str(tmp_path / "cache/custom-whisper")})
    assert calls["transcriptions"][0][1] == {
        "language": "zh", "beam_size": 9, "vad_filter": True, "condition_on_previous_text": False,
        "word_timestamps": False, "initial_prompt": None}
    for path, item in zip(audio, outputs):
        assert isinstance(item, AsrBatchItem) and item.source_audio_path == path
        payload = json.loads(item.output_paths.json_path.read_text())
        assert payload["source_file"] == path.name
        assert payload["full_text"] == item.output_paths.txt_path.read_text() == "固定边界结果。"
        assert payload["segments"] == [{"id": 7, "start": .1, "end": .9, "text": "固定边界结果。"}]
        assert payload["metadata"]["candidate"] == "whisper-existing"
        assert payload["metadata"]["source_audio_sha256"]
    single = file_asr.transcribe_file(audio[0], tmp_path / "single", options=options, resources=resources)
    assert single.output_paths.txt_path.read_text() == "固定边界结果。"


def test_project_asr_adapter_passes_only_operation_parameters_and_resources(tmp_path, monkeypatch):
    from src.asr_utils import transcribe_batch
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    loaded.active_profile.beam_size = 11
    loaded.settings.asr.condition_on_previous_text = False
    loaded.settings.asr.initial_prompt = ""
    loaded.settings.asr.terms = ["专名"]
    audio = loaded.path_for("audio_dir") / "source.wav"
    audio.parent.mkdir(parents=True)
    audio.touch()
    sentinel = []
    def run(paths, output_dir, *, options, resources, logger):
        assert paths == [audio] and output_dir == loaded.path_for("asr_dir")
        assert isinstance(options, FileAsrOptions) and isinstance(resources, AsrResources)
        assert options.beam_size == 11 and options.condition_on_previous_text is False
        assert options.initial_prompt == "" and options.terms == ("专名",)
        assert resources.source_root == tmp_path
        assert resources.gpu_lock_path == tmp_path / "data/jobs/_asr-gpu.lock"
        return sentinel
    monkeypatch.setattr(file_asr, "transcribe_files", run)
    assert transcribe_batch(loaded) is sentinel


def test_standalone_cuda_operation_requires_explicit_shared_lock(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("missing lock must fail before model loading")
    monkeypatch.setattr(file_asr, "load_faster_whisper_model", unexpected)
    with pytest.raises(AsrTranscriptionError, match="gpu_lock_path"):
        file_asr.transcribe_file(tmp_path / "a.wav", tmp_path / "out",
            options=FileAsrOptions("tiny", "cuda", "float16"), resources=AsrResources(tmp_path / "cache"))


@pytest.mark.parametrize("values", [{"max_new_tokens": 0}, {"max_new_tokens": 8193},
                                    {"chunk_seconds": 0}, {"chunk_seconds": 31}, {"chunk_seconds": float("nan")}])
def test_standalone_file_parameters_retain_existing_backend_limits(values):
    with pytest.raises(ValueError):
        FileAsrOptions("tiny", "cpu", "int8", **values)


def test_optional_batch_launches_one_worker_with_caller_relative_paths_and_actual_locks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", SimpleNamespace(
        decode_audio=lambda *args, **kwargs: np.ones(16000, dtype=np.float32)))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", SimpleNamespace(
        get_speech_timestamps=lambda pcm: [{"start": 0, "end": len(pcm)}]))
    files = [Path(name) for name in ("a.wav", "b.wav")]
    for path in files:
        path.write_bytes(b"fixture audio")
    requests = []
    def run(command, **kwargs):
        if "-c" in command:
            return subprocess.CompletedProcess(command, 0)
        assert command[1:3] == ["-m", "src.asr.worker"]
        # The new native entry accepts caller-relative paths, while the worker
        # must import from the repository root and retain its venv interpreter.
        assert Path(command[0]).is_absolute() and Path(command[0]).is_symlink()
        assert kwargs["cwd"] != tmp_path
        assert Path(command[command.index("--request") + 1]).is_absolute()
        assert Path(command[command.index("--response") + 1]).is_absolute()
        assert len(kwargs["pass_fds"]) == 2  # workspace + shared GPU mutex
        import os
        for fd in kwargs["pass_fds"]:
            os.fstat(fd)
        request = json.loads(Path(command[command.index("--request") + 1]).read_text())
        requests.append(request)
        payloads = []
        for item in request["audio"]:
            payloads.append({"source_file": item["source_file"], "engine": "qwen-asr", "model_size": "pinned",
                "device": "cuda", "compute_type": "float16", "language": "zh",
                "segments": [{"id": 1, "start": 0, "end": 1, "text": "固定短句。"}], "full_text": "固定短句。",
                "metadata": {"candidate": "qwen3-asr-0.6b", "duration_seconds": 1}})
        Path(command[command.index("--response") + 1]).write_text(json.dumps(payloads))
        return subprocess.CompletedProcess(command, 0, stderr="")
    monkeypatch.setattr(file_asr.subprocess, "run", run)
    options = FileAsrOptions("unused-whisper", "cuda", "unused-whisper", candidate="qwen3-asr-0.6b",
                             terms=(" 名词 ", "名词"), max_new_tokens=512, chunk_seconds=15)
    Path("worker-python").symlink_to(sys.executable)
    Path("actual-model-cache").mkdir()
    Path("models").symlink_to("actual-model-cache", target_is_directory=True)
    resources = AsrResources(Path("models"), worker_python="worker-python",
                             gpu_lock_path=Path("shared/_asr-gpu.lock"), source_root=Path("."))
    outputs = file_asr.transcribe_files(files, Path("out"), options=options, resources=resources)
    assert len(outputs) == 2 and len(requests) == 1
    assert [item["path"] for item in requests[0]["audio"]] == [str(path.resolve()) for path in files]
    assert [item["source_file"] for item in requests[0]["audio"]] == [path.name for path in files]
    assert requests[0]["terms"] == ["名词"]
    assert requests[0]["cache"] == str(tmp_path / "models/asr-models")
    assert requests[0]["parameters"] == {"max_new_tokens": 512, "chunk_seconds": 15}


def test_optional_worker_reuses_one_backend_for_all_input_files(monkeypatch):
    from src.asr import worker
    from src.asr import qwen3_backend
    calls = []
    class Backend:
        def __init__(self, *args):
            calls.append(("load", args))
        def transcribe(self, pcm, rate, duration, terms, regions):
            calls.append(("transcribe", terms))
            return [{"id": 1, "start": 0, "end": duration, "text": "固定。"}], {}
    monkeypatch.setattr(qwen3_backend, "QwenBackend", Backend)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda _: None,
                                                              version=SimpleNamespace(cuda=None)))
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=np.ones(16000, dtype="<f4").tobytes()))
    item = {"path": "fixture.wav", "source_file": "fixture.wav", "speech_detected": True,
            "speech_regions_samples": [{"start": 0, "end": 16000}]}
    result = worker.execute({"candidate": "qwen3-asr-0.6b", "device": "cpu", "cache": "unused",
                             "parameters": {}, "terms": ["专名"], "warnings": [], "audio": [item, item]})
    assert [kind for kind, _ in calls] == ["load", "transcribe", "transcribe"]
    assert all(output["metadata"]["model_load_shared_for_stage"] for output in result)


def test_streaming_capability_keeps_padding_outside_caller_pcm(tmp_path, monkeypatch):
    for name in MODEL_FILES:
        (tmp_path / name).touch()
    accepted = []
    stream = SimpleNamespace(accept_waveform=lambda rate, pcm: accepted.append((rate, pcm.copy())),
                             input_finished=lambda: None)
    recognizer = SimpleNamespace(create_stream=lambda: stream, is_ready=lambda _: False,
                                  get_result=lambda _: "预览")
    settings = {}
    def create(**kwargs):
        settings.update(kwargs)
        return recognizer
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OnlineRecognizer=SimpleNamespace(from_paraformer=create)))
    recognizer = StreamingASR(tmp_path, num_threads=3)
    pcm = np.array([.1, .2, .3], dtype=np.float32)
    assert recognizer.feed(pcm, finish=True) == "预览"
    assert settings["num_threads"] == 3 and settings["enable_endpoint_detection"] is False
    assert [len(values) for _, values in accepted] == [3, 10560]
    np.testing.assert_array_equal(accepted[0][1], pcm)
    assert len(pcm) == 3 and not accepted[1][1].any()


def test_utterance_capability_keeps_local_no_aligner_contract(tmp_path, monkeypatch):
    from src.asr import utterance
    calls = {}
    def backend(candidate, device, cache, parameters, **kwargs):
        calls.update(candidate=candidate.id, device=device, cache=cache, parameters=parameters, **kwargs)
        def transcribe(**kwargs):
            calls["transcription"] = kwargs
            return [SimpleNamespace(text=" 固定短句。 ")]
        return SimpleNamespace(model=SimpleNamespace(transcribe=transcribe))
    monkeypatch.setattr(utterance, "QwenBackend", backend)
    source = tmp_path / "utterance.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(np.ones(160, dtype="<i2").tobytes())
    model = UtteranceASR(tmp_path / "cache")
    assert model.transcribe(source, ["甲", "乙"]) == "固定短句。"
    assert calls["candidate"] == "qwen3-asr-0.6b" and calls["device"] == "cuda"
    assert calls["parameters"] == {"max_new_tokens": 512}
    assert calls["timestamps"] is False and calls["local_files_only"] is True
    assert calls["transcription"]["language"] == "Chinese"
    assert calls["transcription"]["context"] == "甲、乙"
    assert calls["transcription"]["return_time_stamps"] is False
    assert len(calls["transcription"]["audio"][0]) == 160


def test_old_result_exception_and_recognizer_imports_remain_identical():
    import src.asr_utils as legacy
    from src.dictation.final_asr import FinalASR
    from src.dictation.streaming_asr import StreamingASR as LegacyStreaming
    assert legacy.AsrFileResult is AsrFileResult
    assert legacy.AsrSegmentResult is AsrSegmentResult
    assert legacy.AsrTranscriptionError is AsrTranscriptionError
    assert FinalASR is UtteranceASR and LegacyStreaming is StreamingASR

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from src.asr.registry import CANDIDATES, get_candidate, worker_python
from src.asr.segmentation import aligned_segments, bounded_speech_intervals
from src.asr_utils import (AsrDependencyError, AsrFileResult, AsrSegmentResult, AsrTranscriptionError,
                           build_asr_output_paths, check_output_candidate, gpu_asr_lock, write_asr_result,
                           transcribe_optional_backend)
from src.config_loader import load_settings
from src.job_runner import build_job_paths, write_job_settings
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides, apply_model_overrides_to_raw_settings
from src.web.frontend_settings import FrontendSettingsUpdate, save_frontend_settings
from src.web.models import SingleJobRequest, BatchJobRequest, StageRunRequest, StageFileRunRequest, JobRerunRequest
from tests.helpers import write_minimal_settings


@pytest.mark.parametrize("candidate", list(CANDIDATES))
def test_registry_and_schema_agree(tmp_path, candidate):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    apply_model_overrides(loaded, ModelOverrides(asr_candidate=candidate))
    assert loaded.settings.asr.candidate == get_candidate(candidate).id
    assert get_candidate(None).id == "whisper-existing"


@pytest.mark.parametrize("candidate", ["", "qwen", "../../model", "funasr"])
def test_invalid_candidate_does_not_mutate_settings(tmp_path, candidate):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    with pytest.raises(SettingsOverrideError):
        apply_model_overrides(loaded, ModelOverrides(asr_candidate=candidate))
    assert loaded.settings.asr.candidate is None


def test_asr_overrides_do_not_touch_llm_or_whisper_profile(tmp_path):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    before = loaded.active_profile.model_dump()
    llm = loaded.settings.llm.model
    apply_model_overrides(loaded, ModelOverrides(asr_candidate="qwen3-asr-1.7b"))
    assert loaded.active_profile.model_dump() == before
    assert loaded.settings.llm.model == llm
    raw = {"asr": {"engine": "faster-whisper"}}
    apply_model_overrides_to_raw_settings(raw, ModelOverrides(asr_candidate="fun-asr-nano"))
    assert raw == {"asr": {"engine": "faster-whisper", "candidate": "fun-asr-nano"}}


@pytest.mark.parametrize("request_type,fields", [
    (SingleJobRequest, {"video": "v", "output_dir": "o"}),
    (BatchJobRequest, {}), (StageRunRequest, {}),
    (StageFileRunRequest, {"input_files": {"audio": "a"}, "result_name": "r"}),
    (JobRerunRequest, {"start_stage": "transcribe"}),
])
def test_request_to_task_snapshot_chain(tmp_path, request_type, fields):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    request = request_type(**fields, asr_candidate="paraformer-zh", model="llm-is-independent")
    paths = build_job_paths(tmp_path, "chosen")
    output = write_job_settings(project_root=tmp_path, loaded_settings=loaded, job_paths=paths,
                                profile_name="local_cpu", book_name="术语书名",
                                model_overrides=ModelOverrides(asr_candidate=request.asr_candidate, llm_model=request.model))
    snapshot = load_settings(settings_path=output, project_root=tmp_path)
    assert get_candidate(snapshot.settings.asr.candidate).engine == "funasr"
    assert snapshot.settings.llm.model == "llm-is-independent"
    assert "术语书名" in snapshot.settings.asr.terms
    assert loaded.settings.asr.candidate is None


def test_snapshots_are_independent_and_global_changes_do_not_reselect_old_job(tmp_path):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    outputs = []
    for identifier, candidate in [("a", "qwen3-asr-0.6b"), ("b", "fun-asr-nano")]:
        paths = build_job_paths(tmp_path, identifier)
        outputs.append(write_job_settings(project_root=tmp_path, loaded_settings=loaded, job_paths=paths,
                       profile_name="local_cpu", model_overrides=ModelOverrides(asr_candidate=candidate)))
    raw = yaml.safe_load(loaded.settings_path.read_text())
    raw["asr"]["candidate"] = "paraformer-zh"
    loaded.settings_path.write_text(yaml.safe_dump(raw))
    assert [load_settings(settings_path=p, project_root=tmp_path).settings.asr.candidate for p in outputs] == ["qwen3-asr-0.6b", "fun-asr-nano"]
    old = build_job_paths(tmp_path, "old")
    old_settings = write_job_settings(project_root=tmp_path, loaded_settings=loaded, job_paths=old,
                                      profile_name="local_cpu")
    assert load_settings(settings_path=old_settings, project_root=tmp_path).settings.asr.candidate is None


@pytest.mark.parametrize("saved", [None, "qwen3-asr-0.6b"])
def test_historical_web_rerun_ignores_changed_asr_and_profile_defaults(tmp_path, monkeypatch, saved):
    from api_server import create_app
    from src.web.tasks import run_job_rerun
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    paths = build_job_paths(tmp_path, "history")
    output = write_job_settings(project_root=tmp_path, loaded_settings=loaded, job_paths=paths,
                                profile_name="local_cpu", model_overrides=ModelOverrides(asr_candidate=saved))
    raw = yaml.safe_load(output.read_text())
    raw["pipeline"] = {"stages": ["transcribe"]}
    output.write_text(yaml.safe_dump(raw))
    paths.manifest_path.write_text(json.dumps({"profile": "local_cpu"}))
    before = output.read_bytes()
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(asr_candidate="fun-asr-nano", profile="new-global-profile"))
    seen = []
    monkeypatch.setattr("src.web.tasks.run_stage", lambda stage, settings, *args, **kw: seen.append(
        (stage, settings.settings.asr.candidate, settings.active_profile_name)) or 0)
    app = create_app(project_root=tmp_path)
    state = run_job_rerun(app=app, job_id="history", payload={"start_stage": "transcribe"}, state_path=paths.job_root / "state.json")
    assert state["status"] == "success"
    assert seen == [("transcribe", saved, "local_cpu")]
    rejected = run_job_rerun(app=app, job_id="history", payload={"start_stage": "transcribe", "asr_candidate": "fun-asr-nano"}, state_path=paths.job_root / "state.json")
    assert rejected["status"] == "failed" and "独立阶段" in rejected["error_message"]
    assert len(seen) == 1 and output.read_bytes() == before


def test_stage_snapshot_preserves_effective_asr_profile_for_retry(tmp_path, monkeypatch):
    from api_server import create_app
    from src.web.tasks import execute_stage_run
    from src.web.state_store import create_initial_state, write_json_file, read_json_file
    write_minimal_settings(tmp_path)
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(asr_candidate="qwen3-asr-0.6b", profile="local_cpu"))
    app = create_app(project_root=tmp_path)
    path = app.state.stage_run_state_path("stage")
    write_json_file(path, create_initial_state("stage", "stage"))
    seen=[]
    monkeypatch.setattr("src.web.tasks.run_stage", lambda stage, loaded, *args, **kw: seen.append(
        (loaded.settings.asr.candidate, loaded.active_profile_name)) or 0)
    execute_stage_run(app=app,run_id="stage",stage_name="transcribe",payload={})
    state=read_json_file(path)
    assert state["status"] == "success"
    snapshot=Path(state["request_payload"]["config"])
    assert snapshot != tmp_path / "config/settings.yaml" and snapshot.exists()
    assert load_settings(settings_path=snapshot,project_root=tmp_path).settings.asr.candidate == "qwen3-asr-0.6b"
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(asr_candidate="fun-asr-nano", profile="changed-profile"))
    execute_stage_run(app=app,run_id="stage",stage_name="transcribe",payload=state["request_payload"])
    assert read_json_file(path)["status"] == "success"
    assert seen==[("qwen3-asr-0.6b","local_cpu"),("qwen3-asr-0.6b","local_cpu")]


def test_frontend_default_is_independent_from_llm(tmp_path):
    settings = save_frontend_settings(tmp_path, FrontendSettingsUpdate(asr_candidate="fun-asr-nano", model="llm"))
    assert settings.asr_candidate == "fun-asr-nano" and settings.model == "llm"


def test_worker_python_preserves_venv_symlink(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    path = tmp_path / "venv/bin/python"
    path.parent.mkdir(parents=True)
    path.symlink_to(sys.executable)
    loaded.settings.asr.worker_python = "venv/bin/python"
    monkeypatch.delenv("TRANSCRIPT_ASR_PYTHON", raising=False)
    assert worker_python(loaded) == str(path)
    assert worker_python(loaded) != str(path.resolve())


@pytest.mark.parametrize("units", [[("错", 0, 1)], [("字", -1, 1)], [("字", 1, 0)],
                                    [("字", 0, float("nan"))], [("字", 0, 9)]])
def test_alignment_rejects_mismatched_or_invalid_units(units):
    with pytest.raises(ValueError):
        aligned_segments("字。", units, 2)


def test_alignment_preserves_punctuation_and_real_offsets():
    segments = aligned_segments("中文。Hello!", [("中", 30, 31), ("文", 31, 32), ("Hello", 33, 34)], 40)
    assert segments == [
        {"id": 1, "start": 30, "end": 32, "text": "中文。"},
        {"id": 2, "start": 33, "end": 34, "text": "Hello!"},
    ]


@pytest.mark.parametrize("candidate", ["qwen3-asr-1.7b", "qwen3-asr-0.6b"])
@pytest.mark.parametrize("outcome", ["success", "generate_error", "token_limit", "eos_at_limit"])
def test_qwen_generation_projects_only_last_token_and_restores_head(monkeypatch, tmp_path, candidate, outcome):
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend

    class Head:
        def __init__(self):
            self.hooks = []
            self.seen_shapes = []
            self.weight = np.arange(12, dtype=np.float32).reshape(3, 4)

        def register_forward_pre_hook(self, hook):
            self.hooks.append(hook)
            owner = self

            class Handle:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    owner.hooks.remove(hook)

            return Handle()

        def __call__(self, hidden):
            for hook in self.hooks:
                hidden, = hook(self, (hidden,))
            self.seen_shapes.append(hidden.shape)
            return hidden @ self.weight

    head, aligner_head = Head(), Head()
    hidden = np.arange(2 * 4096 * 3, dtype=np.float32).reshape(2, 4096, 3)
    seen = []

    def generate(**kwargs):
        seen.append(kwargs)
        logits = head(hidden)
        assert logits.shape == (2, 1, 4)
        np.testing.assert_allclose(logits[:, -1, :], (hidden @ head.weight)[:, -1, :])
        # The separate aligner must continue projecting every position.
        assert aligner_head(hidden).shape == (2, 4096, 4)
        if outcome == "generate_error":
            raise RuntimeError("simulated generation failure")
        final_token = 9 if outcome == "token_limit" else 2
        extra = 2 if outcome in ("token_limit", "eos_at_limit") else 1
        return SimpleNamespace(sequences=np.full((2, kwargs["input_ids"].shape[1] + extra), final_token))

    model = SimpleNamespace(
        model=SimpleNamespace(generate=generate, generation_config=SimpleNamespace(eos_token_id=[2]),
                              thinker=SimpleNamespace(lm_head=head)),
        _infer_asr=lambda *args: [],
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        float16="float16", float32="float32", cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=lambda *a, **kw: str(tmp_path)))
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(
        Qwen3ASRModel=SimpleNamespace(from_pretrained=lambda *a, **kw: model)))
    monkeypatch.setitem(sys.modules, "qwen_asr.inference.utils", SimpleNamespace(MAX_FORCE_ALIGN_INPUT_SECONDS=180))
    backend = QwenBackend(get_candidate(candidate), "cuda", tmp_path, {"max_new_tokens": 2})
    kwargs = {"input_ids": np.ones((2, 4096), dtype=np.int64), "max_new_tokens": 2}
    if outcome in ("generate_error", "token_limit"):
        match = "simulated generation failure" if outcome == "generate_error" else "拒绝发布截断转录"
        with pytest.raises(RuntimeError, match=match):
            backend.model.model.generate(**kwargs)
    else:
        backend.model.model.generate(**kwargs)
    assert seen == [kwargs]
    assert head.seen_shapes == [(2, 1, 3)]
    assert head.hooks == [] and aligner_head.hooks == []
    # Full forwards outside generation retain the SDK's original shape.
    assert head(hidden).shape == (2, 4096, 4)


def test_qwen_adapter_records_real_vad_offsets_and_has_no_whisper_parameters():
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend
    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-0.6b")
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float32"
    calls = []
    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append((len(waveform), kwargs))
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        return [SimpleNamespace(text="字。", time_stamps=SimpleNamespace(items=[
            SimpleNamespace(text="字", start_time=0, end_time=len(waveform) / rate)]))]
    backend.model = SimpleNamespace(transcribe=recognize)
    segments, details = backend.transcribe(np.zeros(60), 10, 6, ["术语"], [{"start": 10, "end": 60}])
    assert [(s["start"], s["end"]) for s in segments] == [(1, 3), (3, 5), (5, 6)]
    assert [s["id"] for s in segments] == [1, 2, 3]
    assert details["chunking"]["intervals"] == [[1, 3], [3, 5], [5, 6]]
    assert all(k == {"language": "Chinese", "context": "术语", "return_time_stamps": True} for _, k in calls)
    backend.model.transcribe = lambda **kw: [SimpleNamespace(text="", time_stamps=None)]
    with pytest.raises(RuntimeError, match="拒绝静默遗漏"):
        backend.transcribe(np.zeros(60), 10, 6, [], [{"start": 10, "end": 60}])
    backend.model.transcribe = lambda **kw: [SimpleNamespace(text="字", time_stamps=None)]
    coarse_segments, coarse_details = backend.transcribe(np.zeros(60), 10, 6, [], [{"start": 10, "end": 60}])
    assert [(s["start"], s["end"], s["text"]) for s in coarse_segments] == [(1, 3, "字"), (3, 5, "字"), (5, 6, "字")]
    assert coarse_details["timestamp_granularity"] == "vad_audio_chunk"
    assert len(coarse_details["chunk_timestamp_fallbacks"]) == 3


@pytest.mark.parametrize("failure", ["token_limit", "zero_alignment"])
def test_qwen06_recovery_is_bounded_local_and_observable(failure, caplog):
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend, QwenGenerationLimitError

    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-0.6b")
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float16"
    calls, alignment_calls = [], []

    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append((waveform.copy(), kwargs))
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        if len(calls) == 1 and failure == "token_limit":
            raise QwenGenerationLimitError("budget exhausted")
        text = "こんにちは。" if len(calls) == 1 else "字。"
        return [SimpleNamespace(text=text, language="Chinese", time_stamps=SimpleNamespace(items=[
            SimpleNamespace(text=text[:-1], start_time=0,
                            end_time=0 if len(calls) == 1 else len(waveform) / rate)]))]

    def align(audio, text, language):
        alignment_calls.append((audio, text, language))
        return [SimpleNamespace(items=[SimpleNamespace(text=text[:-1], start_time=0, end_time=2)])]

    backend.model = SimpleNamespace(transcribe=recognize, forced_aligner=SimpleNamespace(align=align))
    audio = np.arange(60)
    regions = [{"start": 10, "end": 50}]
    terms = ["术语", "词表"]
    segments, details = backend.transcribe(audio, 10, 6, terms, regions)
    np.testing.assert_array_equal(calls[0][0], audio[10:30])
    np.testing.assert_array_equal(calls[-1][0], audio[30:50])
    assert calls[0][1] == calls[-1][1] == {
        "language": "Chinese", "context": "术语、词表", "return_time_stamps": True}
    recovery, = details["chunk_recoveries"]
    if failure == "token_limit":
        assert len(calls) == 3 and alignment_calls == []
        np.testing.assert_array_equal(calls[1][0], calls[0][0])
        assert calls[1][1] == {"language": "Chinese", "context": "", "return_time_stamps": True}
        assert recovery["retry"] == {"stage": "asr", "language": "Chinese", "context_terms_count": 0}
        assert "同模型重试一次" in caplog.text
    else:
        assert len(calls) == 2 and len(alignment_calls) == 1
        aligned_audio, aligned_text, aligned_language = alignment_calls[0]
        np.testing.assert_array_equal(aligned_audio[0], calls[0][0])
        assert aligned_audio[1] == 10 and aligned_text == "こんにちは。" and aligned_language == "Japanese"
        assert recovery["retry"] == {"stage": "alignment", "language": "Japanese", "text_changed": False}
        assert "重新对齐一次" in caplog.text
    assert terms == ["术语", "词表"]
    assert [(s["id"], s["start"], s["end"], s["text"]) for s in segments] == [
        (1, 1, 3, "字。" if failure == "token_limit" else "こんにちは。"), (2, 3, 5, "字。")]
    assert recovery["interval"] == [1, 3] and recovery["retry_count"] == 1
    assert recovery["reason"] == ("generation_token_limit" if failure == "token_limit" else "zero_duration_alignment")
    assert recovery["first_attempt"] == {"language": "Chinese", "context_terms_count": 2}
    assert details["resolved_parameters"]["context_terms"] == terms
    assert details["chunking"]["inference_intervals_including_tail_padding"] == [[1, 3], [3, 5]]
    assert any("人工复核" in warning for warning in details["warnings"])
    # Recovery state belongs to this call, not a subsequent file in the worker.
    _, next_details = backend.transcribe(audio, 10, 6, terms, regions)
    assert next_details["chunk_recoveries"] == []


@pytest.mark.parametrize("retry_failure", ["token_limit", "oom"])
def test_qwen06_failed_context_retry_preserves_both_errors_and_stops(retry_failure):
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend, QwenGenerationLimitError

    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-0.6b")
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float16"
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise QwenGenerationLimitError("original token limit")
        if retry_failure == "token_limit":
            raise QwenGenerationLimitError("retry token limit")
        if retry_failure == "oom":
            raise RuntimeError("CUDA out of memory")
        raise AssertionError("unexpected retry scenario")

    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError, match="首试失败: original token limit；一次恢复仍失败"):
        backend.transcribe(np.zeros(40), 10, 4, ["术语"], [{"start": 0, "end": 40}])
    assert len(calls) == 2  # no third attempt and no next chunk


@pytest.mark.parametrize("failure", ["oom", "generic", "sdk_value_error", "empty", "punctuation"])
def test_qwen06_does_not_retry_unrelated_failures(failure):
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend

    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-0.6b")
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float16"
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        if failure in ("oom", "generic"):
            raise RuntimeError("CUDA out of memory" if failure == "oom" else "SDK failure")
        if failure == "sdk_value_error":
            raise ValueError("SDK invalid input")
        return [SimpleNamespace(text="" if failure == "empty" else "。！？", time_stamps=None)]

    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError, match="分块"):
        backend.transcribe(np.zeros(20), 10, 2, ["术语"], [{"start": 0, "end": 20}])
    assert len(calls) == 1


def test_qwen17_does_not_retry_generation_limit():
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend, QwenGenerationLimitError

    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-1.7b")
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float16"
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        raise QwenGenerationLimitError("token limit")

    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError):
        backend.transcribe(np.zeros(20), 10, 2, ["术语"], [{"start": 0, "end": 20}])
    assert len(calls) == 1


@pytest.mark.parametrize("text,expected", [("みなさんこんにちは。", True), ("皆さんこんばんは。", True),
                                           ("コーヒー。", True), ("中文。", False), ("中文ABCこんにちは", False),
                                           ("안녕하세요", False), ("。", False)])
def test_qwen_japanese_tokenizer_routing_is_conservative(text, expected):
    from src.asr.qwen3_backend import _uses_japanese_script
    assert _uses_japanese_script(text) is expected


@pytest.mark.parametrize("second_error", ["oom", "sdk_value_error"])
def test_qwen06_realign_sdk_failure_is_not_swallowed_or_retried(second_error):
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend

    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-0.6b")
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float16"
    calls = []
    text = "こんにちは"

    def recognize(**kwargs):
        calls.append("asr")
        return [SimpleNamespace(text=text, time_stamps=SimpleNamespace(items=[
            SimpleNamespace(text=text, start_time=0, end_time=0)]))]

    def align(**kwargs):
        calls.append("alignment")
        if second_error == "oom":
            raise RuntimeError("CUDA out of memory")
        raise ValueError("SDK invalid input")

    backend.model = SimpleNamespace(transcribe=recognize, forced_aligner=SimpleNamespace(align=align))
    with pytest.raises(RuntimeError, match="一次恢复仍失败"):
        backend.transcribe(np.zeros(40), 10, 4, [], [{"start": 0, "end": 40}])
    assert calls == ["asr", "alignment"]


def test_qwen06_without_terms_does_not_retry_same_generation():
    import numpy as np
    from src.asr.qwen3_backend import QwenBackend, QwenGenerationLimitError

    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate("qwen3-asr-0.6b")
    backend.chunk_seconds = 2
    calls = []
    def recognize(**kwargs):
        calls.append(kwargs)
        raise QwenGenerationLimitError("token limit")
    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError, match="token limit"):
        backend.transcribe(np.zeros(20), 10, 2, [], [{"start": 0, "end": 20}])
    assert len(calls) == 1


def test_zero_duration_phrases_merge_only_with_actual_adjacent_alignment():
    assert aligned_segments("甲。乙。丙。", [("甲", 0, 0), ("乙", 1, 2), ("丙", 2, 2)], 3) == [
        {"id": 1, "start": 0, "end": 2, "text": "甲。乙。丙。"},
    ]
    with pytest.raises(ValueError, match="全部只有零时长"):
        aligned_segments("甲。乙。", [("甲", 1, 1), ("乙", 2, 2)], 3)


def test_qwen_short_tail_regression_30_seconds_plus_272_ms():
    start, end = round(972.272 * 16000), round(1002.544 * 16000)
    intervals = bounded_speech_intervals([{"start": start, "end": end}], end, 480000,
                                        min_tail_samples=16000)
    assert [(a / 16000, b / 16000) for a, b in intervals] == [
        (972.272, 987.408), (987.408, 1002.544)]
    assert sum(b - a for a, b in intervals) == end - start
    assert intervals[0][1] == intervals[1][0]
    assert all(16000 <= b - a <= 480000 for a, b in intervals)


@pytest.mark.parametrize("length", [480000, 480001, 484352, 959999, 960001])
def test_short_tail_rebalancing_preserves_every_real_sample(length):
    intervals = bounded_speech_intervals([{"start": 100, "end": 100 + length}],
                                        length + 100, 480000, min_tail_samples=16000)
    assert intervals[0][0] == 100 and intervals[-1][1] == length + 100
    assert sum(b - a for a, b in intervals) == length
    assert all(a[1] == b[0] for a, b in zip(intervals, intervals[1:]))
    assert all(0 < b - a <= 480000 for a, b in intervals)
    assert intervals[-1][1] - intervals[-1][0] >= 16000


def test_naturally_short_vad_region_is_not_padded_or_joined_across_silence():
    regions = [{"start": 10, "end": 20}, {"start": 50, "end": 60}]
    assert bounded_speech_intervals(regions, 100, 40, min_tail_samples=20) == [(10, 20), (50, 60)]
    assert bounded_speech_intervals([{"start": 0, "end": 41}], 41, 40,
                                    min_tail_samples=40) == [(0, 20), (20, 41)]


def test_vad_chunks_use_real_sample_boundaries_without_gaps_or_overlap():
    regions = [{"start": 200, "end": 500}, {"start": 900, "end": 1600}]
    assert bounded_speech_intervals(regions, 2000, 400) == [(200, 500), (900, 1300), (1300, 1600)]


@pytest.mark.parametrize("regions", [[{"start": -1, "end": 10}], [{"start": 0, "end": 2001}],
                                    [{"start": 0, "end": 500}, {"start": 400, "end": 900}]])
def test_invalid_vad_ranges_are_rejected(regions):
    with pytest.raises(ValueError):
        bounded_speech_intervals(regions, 2000, 400)


@pytest.mark.parametrize("device", ["mps", "bad-device"])
def test_invalid_optional_device_fails_before_loading(tmp_path, device):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    loaded.settings.asr.candidate = "fun-asr-nano"
    loaded.active_profile.device = device
    with pytest.raises(AsrTranscriptionError, match="不支持设备"):
        transcribe_optional_backend([], tmp_path, loaded, None)


def test_optional_missing_dependencies_fails_without_whisper_fallback(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    loaded.settings.asr.candidate = "qwen3-asr-0.6b"
    monkeypatch.setattr("src.asr_utils.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 1))
    with pytest.raises(AsrDependencyError, match="requirements-asr-qwen"):
        transcribe_optional_backend([], tmp_path, loaded, None)


def result(segments=None, full_text="字"):
    return AsrFileResult("audio.wav", "funasr", "pinned-model", "cpu", "float32", "zh",
                         segments if segments is not None else [AsrSegmentResult(1, 0, 1, "字")],
                         full_text, {"candidate": "paraformer-zh", "duration_seconds": 2})


def test_invalid_result_cannot_overwrite_previous_success(tmp_path):
    paths = build_asr_output_paths(Path("audio.wav"), tmp_path)
    write_asr_result(result(), paths)
    before = paths.json_path.read_bytes(), paths.txt_path.read_bytes()
    with pytest.raises(AsrTranscriptionError):
        write_asr_result(result([AsrSegmentResult(1, 0, float("inf"), "字")]), paths)
    assert before == (paths.json_path.read_bytes(), paths.txt_path.read_bytes())


def test_pair_publication_rolls_back_on_second_rename_failure(tmp_path, monkeypatch):
    paths = build_asr_output_paths(Path("audio.wav"), tmp_path)
    write_asr_result(result(), paths)
    before = paths.json_path.read_bytes(), paths.txt_path.read_bytes()
    replace = os.replace
    def fail_second(source, target):
        if Path(target) == paths.txt_path:
            raise OSError("simulated publication error")
        return replace(source, target)
    monkeypatch.setattr("src.asr_utils.os.replace", fail_second)
    with pytest.raises(AsrTranscriptionError):
        write_asr_result(result(full_text="字"), paths)
    assert before == (paths.json_path.read_bytes(), paths.txt_path.read_bytes())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.json", "audio.txt"]


def test_switching_candidate_requires_new_workspace(tmp_path):
    audio = Path("audio.wav")
    paths = build_asr_output_paths(audio, tmp_path)
    paths.json_path.write_text(json.dumps({"engine": "faster-whisper"}))
    check_output_candidate([audio], tmp_path, "whisper-existing")
    with pytest.raises(AsrTranscriptionError, match="独立"):
        check_output_candidate([audio], tmp_path, "fun-asr-nano")


def test_gpu_lock_survives_parent_reference_close(tmp_path):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    loaded.active_profile.device = "cuda"
    child = None
    try:
        with gpu_asr_lock(loaded) as fd:
            child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready',flush=True); sys.stdin.read()"],
                                     pass_fds=(fd,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == "ready"
        with (tmp_path / "data/jobs/_asr-gpu.lock").open("a") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child.stdin.close()
            child.wait(timeout=5)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()

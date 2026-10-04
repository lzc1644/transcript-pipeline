from types import SimpleNamespace

import numpy as np
import pytest

from src.asr.qwen3_backend import QwenBackend, QwenGenerationLimitError
from src.asr.registry import ALIGNER_MODEL, get_candidate
from src.asr_utils import AsrFileResult, AsrSegmentResult, build_asr_output_paths, write_asr_result
from src.align_utils import load_asr_payload
from src.refine_utils import load_text_file


def backend_for(candidate="qwen3-asr-0.6b"):
    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate(candidate)
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = 2, 180, 2048, "float16"
    backend.chunk_intervals = [[0, 2]]
    return backend


def recognition(text, units):
    return [SimpleNamespace(text=text, time_stamps=None if units is None else SimpleNamespace(items=[
        SimpleNamespace(text=t, start_time=s, end_time=e) for t, s, e in units]))]


@pytest.mark.parametrize("candidate", ["qwen3-asr-0.6b", "qwen3-asr-1.7b"])
@pytest.mark.parametrize("text", ["中文 Hello!", "中文こんにちは。", "我们说 Bonjour。", "中文안녕하세요。", "中文Привет。", "中文مرحبا。"])
def test_valid_mixed_language_is_preserved_without_language_switch(candidate, text):
    backend = backend_for(candidate)
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        return recognition(text, [(text, .2, 1.8)])

    backend.model = SimpleNamespace(transcribe=recognize)
    segments, details = backend.transcribe(np.arange(40), 10, 4, ["术语"], [{"start": 10, "end": 30}])
    assert segments == [{"id": 1, "start": 1.2, "end": 2.8, "text": text}]
    assert len(calls) == 1 and calls[0]["language"] == "Chinese" and calls[0]["context"] == "术语"
    assert details["chunk_timestamp_fallbacks"] == details["chunk_recoveries"] == []
    assert details["timestamp_source"] == ALIGNER_MODEL
    assert details["timestamp_granularity"] == "text_segment_from_character_or_word_alignment"


@pytest.mark.parametrize("candidate", ["qwen3-asr-0.6b", "qwen3-asr-1.7b"])
@pytest.mark.parametrize("invalid", ["missing", "empty", "zero", "mismatch", "negative", "reversed", "nan", "order", "bounds"])
def test_invalid_alignment_uses_exact_pcm_chunk_not_guessed_text_timing(candidate, invalid, caplog, tmp_path):
    backend = backend_for(candidate)
    text = "中文 Hello!"
    units = {
        "missing": None, "empty": [], "zero": [(text, .8, .8)],
        "mismatch": [("中文 Other", .2, 1.8)], "negative": [(text, -.1, 1)],
        "reversed": [(text, 1, .5)], "nan": [(text, 0, float("nan"))],
        "order": [("中文", 1, 1.5), ("Hello", .5, 1)], "bounds": [(text, 0, 8)],
    }[invalid]
    calls = []
    audio = np.arange(60)

    def recognize(**kwargs):
        calls.append(kwargs)
        return recognition(text, units if len(calls) == 1 else [(text, .2, 1.8)])

    backend.model = SimpleNamespace(transcribe=recognize)
    segments, details = backend.transcribe(audio, 10, 6, ["术语"], [{"start": 10, "end": 50}])
    assert segments == [
        {"id": 1, "start": 1, "end": 3, "text": text},
        {"id": 2, "start": 3.2, "end": 4.8, "text": text},
    ]
    assert len(calls) == 2  # no extra recognition or alignment attempt
    np.testing.assert_array_equal(calls[0]["audio"][0], audio[10:30])
    np.testing.assert_array_equal(calls[1]["audio"][0], audio[30:50])
    assert all(c["language"] == "Chinese" and c["context"] == "术语" for c in calls)
    fallback, = details["chunk_timestamp_fallbacks"]
    assert fallback["interval"] == [1, 3] and fallback["segment_ids"] == [1]
    assert fallback["requires_review"] and not fallback["text_changed"]
    assert fallback["alignment_error"]
    assert fallback["timestamp_source"] == "silero_vad_pcm_slice"
    assert fallback["timestamp_granularity"] == "vad_audio_chunk"
    assert details["timestamp_source"] == "mixed_forced_alignment_and_vad_pcm_slice"
    assert details["timestamp_granularity"] == "mixed_alignment_and_vad_audio_chunk"
    assert details["chunk_recoveries"] == [] and details["zero_duration_alignment_units"] == 0
    assert "粗粒度时间戳" in caplog.text
    assert any("不是字/词/句边界" in warning for warning in details["warnings"])
    # Actual JSON/TXT publication and existing downstream readers still work.
    details["duration_seconds"] = 6
    result = AsrFileResult("sample.wav", "qwen-asr", backend.candidate.model, "cuda", "float16", "zh",
                           [AsrSegmentResult(**s) for s in segments], "\n".join(s["text"] for s in segments), details)
    paths = build_asr_output_paths(tmp_path / "sample.wav", tmp_path)
    write_asr_result(result, paths)
    assert load_asr_payload(paths.json_path)["metadata"]["chunk_timestamp_fallbacks"] == [fallback]
    assert load_text_file(paths.txt_path, "ASR") == result.full_text
    _, next_details = backend.transcribe(audio, 10, 6, [], [{"start": 10, "end": 30}])
    assert next_details["chunk_timestamp_fallbacks"] == []  # no cross-file contamination


def test_all_coarse_output_reports_real_timestamp_source():
    backend = backend_for()
    backend.model = SimpleNamespace(transcribe=lambda **kw: recognition("中文Bonjour。", None))
    segments, details = backend.transcribe(np.zeros(40), 10, 4, [], [{"start": 10, "end": 30}])
    assert segments == [{"id": 1, "start": 1, "end": 3, "text": "中文Bonjour。"}]
    assert details["timestamp_source"] == "silero_vad_pcm_slice"
    assert details["timestamp_granularity"] == "vad_audio_chunk"


def test_generation_recovery_can_use_coarse_timing_without_third_inference():
    backend = backend_for()
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise QwenGenerationLimitError("original token limit")
        return recognition("中文こんにちは。", [("中文こんにちは", 0, 0)])

    backend.model = SimpleNamespace(transcribe=recognize)
    segments, details = backend.transcribe(np.zeros(20), 10, 2, ["术语"], [{"start": 0, "end": 20}])
    assert len(calls) == 2 and calls[1]["context"] == "" and calls[1]["language"] == "Chinese"
    assert segments == [{"id": 1, "start": 0, "end": 2, "text": "中文こんにちは。"}]
    recovery, = details["chunk_recoveries"]
    assert recovery["reason"] == "generation_token_limit"
    assert len(details["chunk_timestamp_fallbacks"]) == 1
    assert details["resolved_parameters"]["context_terms"] == ["术语"]


@pytest.mark.parametrize("units", [[], [("こんにちは", 0, 0)], [("さようなら", 0, 1)], [("こんにちは", 0, 9)]])
def test_invalid_japanese_realign_result_falls_back_without_text_change(units):
    backend = backend_for()
    calls = []
    text = "こんにちは。"

    def recognize(**kwargs):
        calls.append("asr")
        return recognition(text, [("こんにちは", 0, 0)])

    def align(**kwargs):
        calls.append("align")
        assert kwargs["text"] == text and kwargs["language"] == "Japanese"
        return [recognition(text, units)[0].time_stamps]

    backend.model = SimpleNamespace(transcribe=recognize, forced_aligner=SimpleNamespace(align=align))
    segments, details = backend.transcribe(np.zeros(20), 10, 2, [], [{"start": 0, "end": 20}])
    assert calls == ["asr", "align"]
    assert segments == [{"id": 1, "start": 0, "end": 2, "text": text}]
    recovery, = details["chunk_recoveries"]
    assert recovery["reason"] == "zero_duration_alignment" and recovery["first_error"]
    assert len(details["chunk_timestamp_fallbacks"]) == 1

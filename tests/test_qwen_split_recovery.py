from types import SimpleNamespace

import numpy as np
import pytest

from src.align_utils import load_asr_payload
from src.asr.qwen3_backend import QwenBackend, QwenGenerationLimitError
from src.asr.registry import ALIGNER_MODEL, get_candidate
from src.asr_utils import AsrFileResult, AsrSegmentResult, build_asr_output_paths, write_asr_result
from src.refine_utils import load_text_file


def backend_for(candidate="qwen3-asr-0.6b", chunk_seconds=31):
    backend = QwenBackend.__new__(QwenBackend)
    backend.candidate = get_candidate(candidate)
    backend.chunk_seconds, backend.chunk_limit, backend.limit, backend.dtype = chunk_seconds, 180, 2048, "float16"
    return backend


def recognition(text, duration, *, coarse=False):
    return [SimpleNamespace(text=text, time_stamps=None if coarse else SimpleNamespace(items=[
        SimpleNamespace(text=text, start_time=0, end_time=duration)]))]


@pytest.mark.parametrize("length", [300, 301])
def test_split_is_once_sample_exact_local_and_observable(length, caplog, tmp_path):
    backend = backend_for()
    audio = np.arange(400)
    terms = ["术语", "词表"]
    calls = []

    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append((waveform.copy(), kwargs))
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        if len(calls) <= 2:
            raise QwenGenerationLimitError("first limit" if len(calls) == 1 else "context limit")
        return recognition("子块。", len(waveform) / rate)

    backend.model = SimpleNamespace(transcribe=recognize)
    regions = [{"start": 10, "end": 10 + length}, {"start": 350, "end": 370}]
    segments, details = backend.transcribe(audio, 10, 40, terms, regions)
    midpoint = 10 + length // 2
    assert len(calls) == 5  # parent, empty-context retry, two children, next normal block
    for index in [0, 1]:
        np.testing.assert_array_equal(calls[index][0], audio[10:10 + length])
    np.testing.assert_array_equal(calls[2][0], audio[10:midpoint])
    np.testing.assert_array_equal(calls[3][0], audio[midpoint:10 + length])
    np.testing.assert_array_equal(np.concatenate([calls[2][0], calls[3][0]]), calls[0][0])
    np.testing.assert_array_equal(calls[4][0], audio[350:370])
    assert [c[1]["context"] for c in calls] == ["术语、词表", "", "术语、词表", "术语、词表", "术语、词表"]
    assert all(c[1]["language"] == "Chinese" and c[1]["return_time_stamps"] for c in calls)
    assert terms == ["术语", "词表"] and backend.limit == 2048
    bounds = [[1, midpoint / 10], [midpoint / 10, (10 + length) / 10], [35, 37]]
    assert [[s["start"], s["end"]] for s in segments] == bounds
    assert [s["id"] for s in segments] == [1, 2, 3]
    recovery, = details["chunk_recoveries"]
    assert recovery["interval"] == [1, (10 + length) / 10]
    assert recovery["retry_count"] == 3 and recovery["requires_review"]
    assert recovery["first_error"] == "first limit"
    assert recovery["context_retry"] == {"stage": "asr", "language": "Chinese", "context_terms_count": 0, "error": "context limit"}
    assert recovery["retry"] == {"stage": "asr_split", "language": "Chinese", "context_terms_count": 2, "strategy": "bisect_pcm_once"}
    assert recovery["children"] == [{"interval": bounds[0], "segment_ids": [1]}, {"interval": bounds[1], "segment_ids": [2]}]
    assert details["chunking"]["intervals"] == [[1, (10 + length) / 10], [35, 37]]
    assert details["chunking"]["effective_intervals"] == bounds
    assert details["chunking"]["inference_intervals_including_tail_padding"] == bounds
    assert details["timestamp_source"] == ALIGNER_MODEL and not details["chunk_timestamp_fallbacks"]
    assert "二分恢复一次" in caplog.text and any("二分恢复" in w for w in details["warnings"])
    result = AsrFileResult("sample.wav", "qwen-asr", backend.candidate.model, "cuda", "float16", "zh",
                          [AsrSegmentResult(**s) for s in segments], "\n".join(s["text"] for s in segments), details)
    paths = build_asr_output_paths(tmp_path / "sample.wav", tmp_path)
    write_asr_result(result, paths)
    assert load_asr_payload(paths.json_path)["metadata"]["chunk_recoveries"] == [recovery]
    assert load_text_file(paths.txt_path, "ASR") == result.full_text
    _, next_details = backend.transcribe(audio, 10, 40, terms, regions)
    assert not next_details["chunk_recoveries"]  # no state leaks between files
    assert next_details["chunking"]["effective_intervals"] == next_details["chunking"]["intervals"]


def test_multiple_split_parents_keep_segment_ids_and_records_separate():
    backend = backend_for()
    calls = []

    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append(kwargs)
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        if len(waveform) == 200:
            raise QwenGenerationLimitError("parent limit")
        if len(waveform) == 100:
            return [SimpleNamespace(text="甲。乙。", time_stamps=SimpleNamespace(items=[
                SimpleNamespace(text="甲", start_time=0, end_time=4),
                SimpleNamespace(text="乙", start_time=5, end_time=10)]))]
        return recognition("正常。", len(waveform) / rate)

    backend.model = SimpleNamespace(transcribe=recognize)
    regions = [{"start": 0, "end": 10}, {"start": 20, "end": 220},
               {"start": 250, "end": 450}, {"start": 460, "end": 480}]
    segments, details = backend.transcribe(np.arange(500), 10, 50, ["术语"], regions)
    assert len(calls) == 10 and [s["id"] for s in segments] == list(range(1, 11))
    first, second = details["chunk_recoveries"]
    assert first["interval"] == [2, 22] and second["interval"] == [25, 45]
    assert first["children"] == [{"interval": [2, 12], "segment_ids": [2, 3]},
                                 {"interval": [12, 22], "segment_ids": [4, 5]}]
    assert second["children"] == [{"interval": [25, 35], "segment_ids": [6, 7]},
                                  {"interval": [35, 45], "segment_ids": [8, 9]}]
    assert first["children"] is not second["children"]
    assert details["chunking"]["effective_intervals"] == [[0, 1], [2, 12], [12, 22], [25, 35], [35, 45], [46, 48]]


@pytest.mark.parametrize("failed_call", [3, 4])
@pytest.mark.parametrize("failure", ["limit", "oom", "sdk", "sdk_value", "empty", "punctuation"])
def test_child_failure_aborts_without_retry_grandchildren_or_next_block(failed_call, failure):
    backend = backend_for()
    calls = []

    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append(kwargs)
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        if len(calls) <= 2:
            raise QwenGenerationLimitError("first limit" if len(calls) == 1 else "context limit")
        if len(calls) == failed_call:
            if failure == "limit":
                raise QwenGenerationLimitError("child limit")
            if failure in ["oom", "sdk"]:
                raise RuntimeError("CUDA out of memory" if failure == "oom" else "SDK failure")
            if failure == "sdk_value":
                raise ValueError("SDK invalid input")
            return recognition("" if failure == "empty" else "。！？", len(waveform) / rate)
        return recognition("有效。", len(waveform) / rate)

    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError) as error:
        backend.transcribe(np.arange(400), 10, 40, ["术语"], [{"start": 10, "end": 310}, {"start": 350, "end": 370}])
    assert "first limit" in str(error.value) and "context limit" in str(error.value)
    assert "二分子块失败" in str(error.value) and "原块 [1.0, 31.0]" in str(error.value)
    assert len(calls) == failed_call
    assert calls[-1]["context"] == "术语"


@pytest.mark.parametrize("failed_call", [1, 2])
@pytest.mark.parametrize("failure", ["oom", "sdk_value", "empty"])
def test_only_two_generation_limit_errors_enable_split(failed_call, failure):
    backend = backend_for()
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        if len(calls) < failed_call:
            raise QwenGenerationLimitError("first limit")
        if failure == "oom":
            raise RuntimeError("CUDA out of memory")
        if failure == "sdk_value":
            raise ValueError("SDK invalid input")
        return recognition("", 30)

    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError):
        backend.transcribe(np.zeros(300), 10, 30, ["术语"], [{"start": 0, "end": 300}])
    assert len(calls) == failed_call


@pytest.mark.parametrize("candidate,terms", [("qwen3-asr-1.7b", ["术语"]), ("qwen3-asr-0.6b", [])])
def test_split_does_not_expand_candidate_or_empty_context_policy(candidate, terms):
    backend = backend_for(candidate)
    calls = []

    def recognize(**kwargs):
        calls.append(kwargs)
        raise QwenGenerationLimitError("limit")

    backend.model = SimpleNamespace(transcribe=recognize)
    with pytest.raises(RuntimeError):
        backend.transcribe(np.zeros(300), 10, 30, terms, [{"start": 0, "end": 300}])
    assert len(calls) == 1


@pytest.mark.parametrize("length,expected_calls", [(10, 2), (19, 2), (20, 4), (21, 4)])
def test_split_minimum_one_second_per_child(length, expected_calls):
    backend = backend_for()
    calls = []

    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append(waveform.copy())
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        if len(calls) <= 2:
            raise QwenGenerationLimitError("limit")
        return recognition("字。", len(waveform) / rate)

    backend.model = SimpleNamespace(transcribe=recognize)
    if expected_calls == 2:
        with pytest.raises(RuntimeError):
            backend.transcribe(np.arange(length), 10, length / 10, ["术语"], [{"start": 0, "end": length}])
    else:
        backend.transcribe(np.arange(length), 10, length / 10, ["术语"], [{"start": 0, "end": length}])
        assert min(len(c) for c in calls[2:]) >= 10
        np.testing.assert_array_equal(np.concatenate(calls[2:]), calls[0])
    assert len(calls) == expected_calls


@pytest.mark.parametrize("coarse_children", [(True, True), (True, False), (False, True)])
def test_split_alignment_fallback_uses_child_bounds_and_correct_file_granularity(coarse_children):
    backend = backend_for()
    calls = []

    def recognize(audio, **kwargs):
        waveform, rate = audio
        calls.append(kwargs)
        backend.chunk_intervals = [[0, len(waveform) / rate]]
        if len(calls) <= 2:
            raise QwenGenerationLimitError("limit")
        # No Japanese re-alignment after split; even zero-duration Japanese units
        # must use the existing coarse timing path, not add another model call.
        result = recognition("こんにちは。", len(waveform) / rate, coarse=coarse_children[len(calls) - 3])
        if coarse_children[len(calls) - 3]:
            result[0].time_stamps = SimpleNamespace(items=[SimpleNamespace(text="こんにちは", start_time=0, end_time=0)])
        return result

    backend.model = SimpleNamespace(transcribe=recognize)
    segments, details = backend.transcribe(np.zeros(320), 10, 32, ["术语"], [{"start": 10, "end": 310}])
    assert len(calls) == 4 and len(segments) == 2
    assert [[s["start"], s["end"]] for s in segments] == [[1, 16], [16, 31]]
    fallbacks = details["chunk_timestamp_fallbacks"]
    assert [f["interval"] for f in fallbacks] == [b for b, coarse in zip([[1, 16], [16, 31]], coarse_children) if coarse]
    assert [f["segment_ids"] for f in fallbacks] == [[i + 1] for i, coarse in enumerate(coarse_children) if coarse]
    assert details["timestamp_source"] == ("silero_vad_pcm_slice" if all(coarse_children) else "mixed_forced_alignment_and_vad_pcm_slice")
    assert details["timestamp_granularity"] == ("vad_audio_chunk" if all(coarse_children) else "mixed_alignment_and_vad_audio_chunk")
    assert len(details["chunk_recoveries"]) == 1

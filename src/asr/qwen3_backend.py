from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Any

from src.asr.registry import ALIGNER_MODEL, ALIGNER_REVISION, Candidate
from src.asr.segmentation import ZeroDurationAlignmentError, aligned_segments, bounded_speech_intervals, lexical_text


class QwenGenerationLimitError(RuntimeError):
    """Generation exhausted its budget without an end token."""


def _uses_japanese_script(text: str) -> bool:
    """Conservative tokenizer routing, not audio language identification."""
    def kana(char: str) -> bool:
        return "\u3041" <= char <= "\u3096" or "\u30a1" <= char <= "\u30fa"

    return any(kana(c) for c in text) and all(
        not c.isalpha() or kana(c) or "\u3400" <= c <= "\u9fff" or c == "ー" for c in text)


class QwenBackend:
    def __init__(self, candidate: Candidate, device: str, cache: Path, parameters: dict[str, Any]):
        import torch
        from huggingface_hub import snapshot_download
        from qwen_asr import Qwen3ASRModel
        from qwen_asr.inference.utils import MAX_FORCE_ALIGN_INPUT_SECONDS

        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用；不自动改用 CPU")
        self.candidate = candidate
        self.dtype = torch.float16 if device == "cuda" else torch.float32
        self.limit = parameters.get("max_new_tokens", 2048)
        self.chunk_limit = MAX_FORCE_ALIGN_INPUT_SECONDS
        self.chunk_seconds = parameters.get("chunk_seconds", 30.0)
        self.model_path = snapshot_download(candidate.model, revision=candidate.revision, cache_dir=str(cache / "hf"),
                                            allow_patterns=["*.json", "*.txt", "*.safetensors"])
        aligner_path = snapshot_download(ALIGNER_MODEL, revision=ALIGNER_REVISION, cache_dir=str(cache / "hf"),
                                        allow_patterns=["*.json", "*.txt", "*.safetensors"])
        location = "cuda:0" if device == "cuda" else "cpu"
        self.model = Qwen3ASRModel.from_pretrained(
            self.model_path, dtype=self.dtype, device_map=location, attn_implementation="sdpa",
            max_inference_batch_size=1, max_new_tokens=self.limit,
            forced_aligner=aligner_path,
            forced_aligner_kwargs={"dtype": self.dtype, "device_map": location, "attn_implementation": "sdpa"},
        )
        original_generate = self.model.model.generate

        def checked_generate(*args: Any, **kwargs: Any) -> Any:
            # The pinned SDK projects every prefill position onto its large
            # vocabulary, but autoregressive generation only uses the last one.
            # Scope the hook to ASR generation; alignment/full forward is unchanged.
            with self.model.model.thinker.lm_head.register_forward_pre_hook(
                lambda _module, inputs: (inputs[0][:, -1:, :],)
            ):
                output = original_generate(*args, **kwargs)
            sequences = output.sequences
            eos = self.model.model.generation_config.eos_token_id
            eos_ids = eos if isinstance(eos, list) else [eos]
            if sequences.shape[1] - kwargs["input_ids"].shape[1] >= self.limit:
                if any(int(row[-1]) not in eos_ids for row in sequences):
                    raise QwenGenerationLimitError("Qwen 解码达到 token 上限，拒绝发布截断转录")
            return output

        self.model.model.generate = checked_generate
        original_infer = self.model._infer_asr
        self.chunk_intervals = []

        def recorded_infer(contexts: Any, wavs: Any, languages: Any) -> Any:
            # Observe the actual chunks passed by the fixed SDK, not text-based
            # timing or an independently guessed splitting algorithm.
            offset = 0.0
            self.chunk_intervals = []
            for wav in wavs:
                length = len(wav) / 16000
                self.chunk_intervals.append([offset, offset + length])
                offset += length
            return original_infer(contexts, wavs, languages)

        self.model._infer_asr = recorded_infer

    def _transcribe_chunk(self, audio: Any, sample_rate: int, *, context: str) -> tuple[Any, list[tuple]]:
        result = self.model.transcribe(audio=(audio, sample_rate), language="Chinese",
                                       context=context, return_time_stamps=True)[0]
        if not result.text.strip():
            raise ValueError("VAD 语音分块返回空文字，拒绝静默遗漏该分块")
        if not lexical_text(result.text):
            raise ValueError("VAD 语音分块只有标点而无可用文字，拒绝静默遗漏该分块")
        units = ([(v.text, v.start_time, v.end_time) for v in result.time_stamps.items]
                 if result.time_stamps else [])
        return result, units

    def transcribe(self, audio: Any, sample_rate: int, duration: float, terms: list[str],
                   speech_regions: list[dict[str, int]]) -> tuple[list[dict], dict]:
        step = round(self.chunk_seconds * sample_rate)
        min_tail = min(sample_rate, step)
        intervals = bounded_speech_intervals(speech_regions, len(audio), step,
                                            min_tail_samples=min_tail)
        segments = []
        zero_units = 0
        sdk_intervals = []
        recoveries = []
        timestamp_fallbacks = []
        effective_intervals = []
        # Children share a local parent record, but cannot enqueue grandchildren.
        pending = deque((begin, finish, None) for begin, finish in intervals)
        while pending:
            begin, finish, split_recovery = pending.popleft()
            offset = begin / sample_rate
            chunk = audio[begin:finish]
            recovery = None
            try:
                try:
                    result, local_units = self._transcribe_chunk(chunk, sample_rate, context="、".join(terms))
                except QwenGenerationLimitError as first_error:
                    # Only remove the observed source of term echo. Keep Chinese:
                    # automatic language ID misclassified this short mixed speech.
                    if self.candidate.id != "qwen3-asr-0.6b" or not terms or split_recovery is not None:
                        raise
                    recovery = {"reason": "generation_token_limit", "first_error": str(first_error),
                                "retry": {"stage": "asr", "language": "Chinese", "context_terms_count": 0}}
                    logging.getLogger(__name__).warning(
                        "%s 分块 [%s, %s] %s；同模型重试一次：不使用术语上下文，保留中文识别",
                        self.candidate.id, offset, finish / sample_rate, first_error)
                    try:
                        result, local_units = self._transcribe_chunk(chunk, sample_rate, context="")
                    except QwenGenerationLimitError as retry_error:
                        midpoint = begin + (finish - begin) // 2
                        # Do not create sub-second fragments or retry SDK/OOM errors.
                        if min(midpoint - begin, finish - midpoint) < sample_rate:
                            raise
                        recovery.update(
                            interval=[offset, finish / sample_rate], retry_count=3,
                            first_attempt={"language": "Chinese", "context_terms_count": len(terms)},
                            reason="generation_token_limit_after_context_retry",
                            context_retry={**recovery["retry"], "error": str(retry_error)},
                            retry={"stage": "asr_split", "language": "Chinese",
                                   "context_terms_count": len(terms), "strategy": "bisect_pcm_once"},
                            children=[], requires_review=True,
                        )
                        logging.getLogger(__name__).warning(
                            "%s 分块 [%s, %s] 无术语恢复仍触顶：%s；按真实 PCM 二分恢复一次，"
                            "子块使用原术语且不再重试，需人工复核",
                            self.candidate.id, offset, finish / sample_rate, retry_error)
                        pending.appendleft((midpoint, finish, recovery))
                        pending.appendleft((begin, midpoint, recovery))
                        continue
                alignment_error = None
                try:
                    # Validate fine alignment without ever publishing invalid units.
                    if not local_units:
                        raise ValueError("Qwen 返回文字但 ForcedAligner 未返回时间戳")
                    current = aligned_segments(result.text, local_units, len(chunk) / sample_rate)
                except ValueError as first_error:
                    alignment_error = first_error
                    # Keep the proven narrow re-alignment before coarse fallback.
                    # No additional inference after a generation retry. SDK/OOM
                    # exceptions are outside validation catches and remain fatal.
                    if (isinstance(first_error, ZeroDurationAlignmentError)
                            and self.candidate.id == "qwen3-asr-0.6b" and recovery is None and split_recovery is None
                            and _uses_japanese_script(result.text)):
                        recovery = {"reason": "zero_duration_alignment", "first_error": str(first_error),
                                    "retry": {"stage": "alignment", "language": "Japanese", "text_changed": False}}
                        logging.getLogger(__name__).warning(
                            "%s 分块 [%s, %s] %s；含假名日文按 Japanese 重新对齐一次，保留原识别文字",
                            self.candidate.id, offset, finish / sample_rate, first_error)
                        alignment = self.model.forced_aligner.align(
                            audio=(chunk, sample_rate), text=result.text, language="Japanese")[0]
                        local_units = [(v.text, v.start_time, v.end_time) for v in alignment.items]
                        try:
                            current = aligned_segments(result.text, local_units, len(chunk) / sample_rate)
                        except ValueError as exc:
                            alignment_error = exc
                        else:
                            alignment_error = None
                if alignment_error is not None:
                    # One whole real PCM slice, NOT guessed word/sentence timing.
                    current = [{"id": 1, "start": 0.0, "end": len(chunk) / sample_rate,
                                "text": result.text.strip()}]
                    timestamp_fallbacks.append({
                        "interval": [offset, finish / sample_rate], "segment_ids": [len(segments) + 1],
                        "reason": "invalid_fine_alignment", "alignment_error": str(alignment_error),
                        "timestamp_source": "silero_vad_pcm_slice", "timestamp_granularity": "vad_audio_chunk",
                        "text_changed": False, "requires_review": True,
                    })
                    local_units = []  # Discard invalid alignment; do not count it as usable.
                    logging.getLogger(__name__).warning(
                        "%s 分块 [%s, %s] %s；保留原文字，降级为真实 PCM 分块粗粒度时间戳，需人工复核",
                        self.candidate.id, offset, finish / sample_rate, alignment_error)
                if recovery is not None:
                    recoveries.append({"interval": [offset, finish / sample_rate], "retry_count": 1,
                                       "first_attempt": {"language": "Chinese", "context_terms_count": len(terms)},
                                       **recovery})
            except Exception as exc:
                if split_recovery is not None:
                    detail = (f"原块 {split_recovery['interval']} 首试失败: {split_recovery['first_error']}；"
                              f"无术语恢复失败: {split_recovery['context_retry']['error']}；二分子块失败: {exc}")
                else:
                    detail = (f"首试失败: {recovery['first_error']}；一次恢复仍失败: {exc}"
                              if recovery is not None else str(exc))
                raise RuntimeError(f"{self.candidate.id} 分块 [{offset}, {finish / sample_rate}] 失败: {detail}") from exc
            if split_recovery is not None:
                split_recovery["children"].append({
                    "interval": [offset, finish / sample_rate],
                    "segment_ids": list(range(len(segments) + 1, len(segments) + len(current) + 1)),
                })
                # Only attest success after BOTH children pass generation and timing.
                if len(split_recovery["children"]) == 2:
                    recoveries.append(split_recovery)
            effective_intervals.append([offset, finish / sample_rate])
            zero_units += sum(start == end for _, start, end in local_units)
            sdk_intervals.extend([[offset + start, offset + end] for start, end in self.chunk_intervals])
            for segment in current:
                segment.update(id=len(segments) + 1, start=segment["start"] + offset, end=segment["end"] + offset)
                segments.append(segment)
        warnings = []
        if any(r["retry"]["stage"] == "asr" for r in recoveries):
            warnings.append("Qwen 0.6B 部分分块解码触顶后经同模型无术语重试恢复；"
                            "仍按中文识别，恢复块的专名需人工复核，详见 chunk_recoveries")
        if any(r["retry"]["stage"] == "asr_split" for r in recoveries):
            warnings.append("Qwen 0.6B 部分分块首试及无术语恢复均触顶后，经同 PCM 二分恢复；"
                            "子块使用原术语且不再重试，分块边界/文字需人工复核，详见 chunk_recoveries")
        if any(r["retry"]["stage"] == "alignment" for r in recoveries):
            warnings.append("Qwen 0.6B 含假名日文的零时长分块已尝试按 Japanese 重新对齐，未改识别文字；"
                            "外语文字及时间戳需人工复核，详见 chunk_recoveries 和 chunk_timestamp_fallbacks")
        if timestamp_fallbacks:
            warnings.append("Qwen 部分分块细粒度对齐无效，已保留原识别文字并使用真实 PCM 分块粗粒度时间戳；"
                            "不是字/词/句边界，需后续 AI 校对与人工复核，详见 chunk_timestamp_fallbacks")
        if zero_units:
            warnings.append("ForcedAligner 存在零时长字/词，已按实际边界聚合文本段；细粒度对齐需人工关注")
        if any(r["end"] - r["start"] > step for r in speech_regions):
            warnings.append("长 VAD 语音区间按真实 PCM 上限切分；分块边界可能截断词语，需人工复核")
        all_coarse = bool(timestamp_fallbacks) and len(timestamp_fallbacks) == len(effective_intervals)
        return segments, {
            "timestamp_source": ("silero_vad_pcm_slice" if all_coarse else
                                 "mixed_forced_alignment_and_vad_pcm_slice" if timestamp_fallbacks else ALIGNER_MODEL),
            "aligner_revision": ALIGNER_REVISION,
            "zero_duration_alignment_units": zero_units, "warnings": warnings,
            "chunk_recoveries": recoveries, "chunk_timestamp_fallbacks": timestamp_fallbacks,
            "timestamp_granularity": ("vad_audio_chunk" if all_coarse else
                                      "mixed_alignment_and_vad_audio_chunk" if timestamp_fallbacks else
                                      "text_segment_from_character_or_word_alignment"),
            "resolved_parameters": {"dtype": str(self.dtype), "batch_size": 1,
                                    "max_new_tokens": self.limit, "context_terms": terms,
                                    "attn_implementation": "sdpa", "generation_logits": "last_token"},
            "chunking": {"strategy": "bounded Silero VAD PCM slices + official SDK", "max_seconds": self.chunk_seconds,
                         "sdk_target_seconds": self.chunk_limit, "overlap": 0,
                         "short_tail_threshold_seconds": min_tail / sample_rate,
                         "short_tail_policy": "balance_last_pair_within_same_vad_region",
                         "intervals": [[begin / sample_rate, finish / sample_rate] for begin, finish in intervals],
                         "effective_intervals": effective_intervals,
                         "inference_intervals_including_tail_padding": sdk_intervals},
        }

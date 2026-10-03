from __future__ import annotations

from pathlib import Path
from typing import Any

from src.asr.registry import ALIGNER_MODEL, ALIGNER_REVISION, Candidate
from src.asr.segmentation import aligned_segments, bounded_speech_intervals


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
                    raise RuntimeError("Qwen 解码达到 token 上限，拒绝发布截断转录")
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

    def transcribe(self, audio: Any, sample_rate: int, duration: float, terms: list[str],
                   speech_regions: list[dict[str, int]]) -> tuple[list[dict], dict]:
        step = round(self.chunk_seconds * sample_rate)
        min_tail = min(sample_rate, step)
        intervals = bounded_speech_intervals(speech_regions, len(audio), step,
                                            min_tail_samples=min_tail)
        segments = []
        zero_units = 0
        sdk_intervals = []
        for begin, finish in intervals:
            offset = begin / sample_rate
            try:
                result = self.model.transcribe(audio=(audio[begin:finish], sample_rate), language="Chinese",
                                               context="、".join(terms), return_time_stamps=True)[0]
                if not result.text.strip():
                    raise ValueError("VAD 语音分块返回空文字，拒绝静默遗漏该分块")
                if not result.time_stamps:
                    raise ValueError("Qwen 返回文字但 ForcedAligner 未返回时间戳")
                local_units = [(v.text, v.start_time, v.end_time) for v in result.time_stamps.items] if result.time_stamps else []
                # Validate against this actual PCM slice before applying offsets.
                current = aligned_segments(result.text, local_units, (finish - begin) / sample_rate)
            except Exception as exc:
                raise RuntimeError(f"{self.candidate.id} 分块 [{offset}, {finish / sample_rate}] 失败: {exc}") from exc
            zero_units += sum(start == end for _, start, end in local_units)
            sdk_intervals.extend([[offset + start, offset + end] for start, end in self.chunk_intervals])
            for segment in current:
                segment.update(id=len(segments) + 1, start=segment["start"] + offset, end=segment["end"] + offset)
                segments.append(segment)
        warnings = []
        if zero_units:
            warnings.append("ForcedAligner 存在零时长字/词，已按实际边界聚合文本段；细粒度对齐需人工关注")
        if any(r["end"] - r["start"] > step for r in speech_regions):
            warnings.append("长 VAD 语音区间按真实 PCM 上限切分；分块边界可能截断词语，需人工复核")
        return segments, {
            "timestamp_source": ALIGNER_MODEL, "aligner_revision": ALIGNER_REVISION,
            "zero_duration_alignment_units": zero_units, "warnings": warnings,
            "timestamp_granularity": "text_segment_from_character_or_word_alignment",
            "resolved_parameters": {"dtype": str(self.dtype), "batch_size": 1,
                                    "max_new_tokens": self.limit, "context_terms": terms,
                                    "attn_implementation": "sdpa", "generation_logits": "last_token"},
            "chunking": {"strategy": "bounded Silero VAD PCM slices + official SDK", "max_seconds": self.chunk_seconds,
                         "sdk_target_seconds": self.chunk_limit, "overlap": 0,
                         "short_tail_threshold_seconds": min_tail / sample_rate,
                         "short_tail_policy": "balance_last_pair_within_same_vad_region",
                         "intervals": [[begin / sample_rate, finish / sample_rate] for begin, finish in intervals],
                         "inference_intervals_including_tail_padding": sdk_intervals},
        }

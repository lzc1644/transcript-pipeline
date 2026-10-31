from __future__ import annotations

from pathlib import Path

from src.asr.registry import get_candidate
from src.asr.segmentation import lexical_text
from src.asr.qwen3_backend import QwenBackend


class UtteranceASR:
    """Qwen whole utterances, without aligner, endpointing or preview prompts."""

    def __init__(self, cache: Path, *, candidate: str = "qwen3-asr-0.6b",
                 device: str = "cuda", max_new_tokens: int = 512, local_files_only: bool = True):
        selected = get_candidate(candidate)
        if selected.engine != "qwen-asr":
            raise ValueError("短句识别要求 Qwen ASR 候选")
        self.backend = QwenBackend(selected, device, cache,
                                   {"max_new_tokens": max_new_tokens}, timestamps=False,
                                   local_files_only=local_files_only)

    def transcribe(self, path: Path, terms: list[str]):
        import wave
        import numpy as np
        with wave.open(str(path)) as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError("最终识别要求 16kHz 单声道 PCM16 WAV")
            pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768
        if not len(pcm):
            raise ValueError("拒绝空音频")
        result = self.backend.model.transcribe(audio=(pcm, 16000), language="Chinese",
                                               context="、".join(terms), return_time_stamps=False)[0]
        if not lexical_text(result.text):
            raise ValueError("语音片段返回空文字或纯标点，未发布最终稿")
        return result.text.strip()

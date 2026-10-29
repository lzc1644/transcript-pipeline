"""Operation parameters and resource locations for file ASR."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FileAsrOptions:
    model_size: str
    device: str
    compute_type: str
    candidate: str | None = None
    secondary_candidate: str | None = None
    engine: str = "faster-whisper"
    language: str = "zh"
    beam_size: int = 5
    vad_filter: bool = False
    condition_on_previous_text: bool = True
    word_timestamps: bool = False
    initial_prompt: str = ""
    max_new_tokens: int = 2048
    chunk_seconds: float = 30.0
    terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # These are the existing file-backend limits, also required without YAML.
        if not 1 <= self.max_new_tokens <= 8192:
            raise ValueError("max_new_tokens 必须在 1 到 8192 之间")
        if not 0 < self.chunk_seconds <= 30:
            raise ValueError("chunk_seconds 必须大于 0 且不超过 30 秒")


@dataclass(frozen=True)
class AsrResources:
    cache_dir: Path
    worker_python: str = field(default_factory=lambda: sys.executable)
    gpu_lock_path: Path | None = None
    source_root: Path | None = None
    whisper_cache_subdir: str = "faster-whisper"
    backend_cache_subdir: str = "asr-models"

    @property
    def whisper_cache_dir(self) -> Path:
        return self.cache_dir / self.whisper_cache_subdir

    @property
    def backend_cache_dir(self) -> Path:
        return self.cache_dir / self.backend_cache_subdir

from __future__ import annotations

from pathlib import Path
from typing import Literal
import json
import os

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.dictation.streaming_asr import MODEL_ID, MODEL_REVISION
from src.asr.registry import get_candidate


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(StrictModel):
    terms: list[str] = Field(default_factory=list, max_length=50)
    cleanup: bool = True

    @field_validator("terms")
    @classmethod
    def validate_terms(cls, terms):
        if any(not t.strip() or len(t) > 80 for t in terms):
            raise ValueError("术语须为 1–80 字，最多 50 项")
        return list(dict.fromkeys(t.strip() for t in terms))


class EditRequest(StrictModel):
    revision: int = Field(ge=0)
    text: str = Field(max_length=10000)
    discard_document: bool = False


class DraftEditRequest(StrictModel):
    revision: int = Field(ge=0)
    text: str = Field(max_length=200000)


class CleanupRequest(StrictModel):
    scope: Literal["failed", "utterance", "document", "apply"] = "failed"
    utterance_id: str | None = None
    revision: int | None = None
    suggestion_id: str | None = None


class DictationSettings(StrictModel):
    enabled: bool = True
    cpu_python: str = ".venv/dictation-py312/bin/python"
    final_python: str = ".venv/asr-py312/bin/python"
    model_dir: str = "data/models/dictation"
    cleanup_model: str = "gpt-6-luna"
    cleanup_timeout_seconds: int = Field(default=20, ge=1, le=120)
    max_upload_mb: int = Field(default=100, ge=1, le=1000)
    max_upload_minutes: int = Field(default=30, ge=1, le=120)
    idle_seconds: int = Field(default=60, ge=10, le=600)
    disconnect_seconds: int = Field(default=10, ge=1, le=60)
    pre_ms: int = Field(default=250, ge=0, le=500)
    silence_ms: int = Field(default=700, ge=300, le=2000)
    tail_ms: int = Field(default=150, ge=0, le=250)
    target_seconds: float = Field(default=20, ge=10, le=20)
    hard_seconds: float = Field(default=25, ge=21, le=25)

    def freeze(self, root: Path, loaded, request: SessionRequest, *, preview=True):
        models = root / self.model_dir
        return {
            **self.model_dump(), **request.model_dump(), "preview": preview,
            "cpu_python": str(root / self.cpu_python), "final_python": str(root / self.final_python),
            "streaming_model": str(models / "paraformer"), "vad_model": str(models / "silero_vad_v6.onnx"),
            "streaming_model_id": MODEL_ID, "streaming_revision": MODEL_REVISION,
            "qwen_model": get_candidate("qwen3-asr-0.6b").model,
            "qwen_revision": get_candidate("qwen3-asr-0.6b").revision,
            "qwen_cache": str(loaded.resolve_path(loaded.active_profile.cache_dir) / loaded.settings.asr.backend_cache_subdir),
            "reasoning_effort": "none", "segmentation": {k: getattr(self, k) for k in
                ("pre_ms", "silence_ms", "tail_ms", "target_seconds", "hard_seconds")},
        }


def load_dictation_settings(root: Path):
    path = root / "config/dictation.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    for field, variable in [("cpu_python", "TRANSCRIPT_DICTATION_CPU_PYTHON"),
                            ("final_python", "TRANSCRIPT_ASR_PYTHON")]:
        if os.getenv(variable):
            raw[field] = os.environ[variable]
    return DictationSettings.model_validate(raw)

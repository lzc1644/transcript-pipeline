from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from src.schemas import AsrCandidateName


class AsrSelection(BaseModel):
    asr_candidate: AsrCandidateName | None = None
    secondary_asr_candidate: AsrCandidateName | Literal[""] | None = None


class SingleJobRequest(AsrSelection):
    video: str
    reference: str | None = None
    output_dir: str
    content_type: Literal["book_club", "conversation"] = "book_club"
    config: str | None = None
    profile: str | None = None
    backend: Literal["codex_api", "codex_cli", "agy", "both"] | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    ocr_backend: Literal["codex_api", "codex_cli", "agy"] | None = None
    ocr_model: str | None = None
    ocr_reasoning_effort: str | None = None
    ocr_max_concurrency: int | None = Field(default=None, ge=1)
    ocr_submit_interval_seconds: float | None = Field(default=None, ge=0)
    book_name: str | None = None
    chapter: str | None = None
    glossary_file: str | None = None
    refine_prompt: str | None = None


class BatchJobRequest(AsrSelection):
    manifest: str | None = None
    videos_dir: str | None = None
    reference_dir: str | None = None
    shared_reference: str | None = None
    output_dir: str | None = None
    content_type: Literal["book_club", "conversation"] = "book_club"
    config: str | None = None
    profile: str | None = None
    backend: Literal["codex_api", "codex_cli", "agy", "both"] | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    ocr_backend: Literal["codex_api", "codex_cli", "agy"] | None = None
    ocr_model: str | None = None
    ocr_reasoning_effort: str | None = None
    ocr_max_concurrency: int | None = Field(default=None, ge=1)
    ocr_submit_interval_seconds: float | None = Field(default=None, ge=0)
    glossary_file: str | None = None
    remote_concurrency: int | None = Field(default=None, ge=1)
    book_name: str | None = None
    chapter: str | None = None
    refine_prompt: str | None = None


class StageRunRequest(AsrSelection):
    config: str | None = None
    profile: str | None = None
    backend: Literal["codex_api", "codex_cli", "agy", "both"] | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    ocr_backend: Literal["codex_api", "codex_cli", "agy"] | None = None
    ocr_model: str | None = None
    ocr_reasoning_effort: str | None = None
    ocr_max_concurrency: int | None = Field(default=None, ge=1)
    ocr_submit_interval_seconds: float | None = Field(default=None, ge=0)


class PDFBookOCRRequest(BaseModel):
    input_path: str = Field(min_length=1)
    config: str | None = None
    ocr_model: str | None = None
    ocr_reasoning_effort: str | None = None
    ocr_max_concurrency: int | None = Field(default=None, ge=1)
    ocr_submit_interval_seconds: float | None = Field(default=None, ge=0)


class StageFileRunRequest(StageRunRequest):
    input_files: dict[str, str]
    result_name: str


class JobRerunRequest(AsrSelection):
    start_stage: str
    profile: str | None = None
    backend: Literal["codex_api", "codex_cli", "agy", "both"] | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    ocr_backend: Literal["codex_api", "codex_cli", "agy"] | None = None
    ocr_model: str | None = None
    ocr_reasoning_effort: str | None = None
    ocr_max_concurrency: int | None = Field(default=None, ge=1)
    ocr_submit_interval_seconds: float | None = Field(default=None, ge=0)

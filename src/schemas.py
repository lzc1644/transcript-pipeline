from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AppBaseModel(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class ProjectSettings(AppBaseModel):
    name: str
    version: str
    description: str


class RuntimeSettings(AppBaseModel):
    profile: str
    environment: str = "local"
    log_level: str = "INFO"


class ProfileSettings(AppBaseModel):
    device: str
    asr_compute_type: str
    asr_model_size: str
    batch_size: int
    beam_size: int | None = None
    temp_dir: str
    cache_dir: str


class PathsSettings(AppBaseModel):
    videos_dir: str
    audio_dir: str
    reference_dir: str
    asr_dir: str
    ocr_dir: str
    extracted_text_dir: str
    chunks_dir: str
    aligned_dir: str
    classified_dir: str
    refined_dir: str
    review_dir: str
    final_dir: str
    logs_dir: str


class AudioSettings(AppBaseModel):
    output_format: str = "wav"
    sample_rate: int = 16000
    channels: int = 1
    overwrite: bool = False
    supported_video_ext: list[str] = Field(default_factory=list)
    supported_audio_ext: list[str] = Field(default_factory=list)

    @field_validator("supported_video_ext", "supported_audio_ext")
    @classmethod
    def normalize_extensions(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            extension = item.strip().lower()
            if not extension.startswith("."):
                extension = f".{extension}"
            normalized.append(extension)
        return normalized


class PromptSettings(AppBaseModel):
    classify_and_correct: str
    final_cleanup: str
    conversation_cleanup: str | None = None


class ReferenceSettings(AppBaseModel):
    enabled: bool = True
    allow_pdf: bool = True
    allow_txt: bool = True
    allow_md: bool = True
    allow_docx: bool = False
    prefer_existing_text: bool = True
    run_ocr_when_needed: bool = False
    sentence_split_enabled: bool = True
    ai_ocr_backend: str = "codex_api"
    gemini_ocr_model: str = "Gemini 3.5 Flash (High)"
    gemini_ocr_fallback_model: str = ""
    codex_ocr_model: str = "gpt-6-luna"
    codex_ocr_reasoning_effort: str = "high"
    codex_ocr_fast_mode: bool = False
    # 用户明确要求默认每 5 秒投递一页、最多 40 个在途请求；两项仍可由任务级设置覆盖。
    codex_ocr_max_concurrency: int = Field(default=40, ge=1)
    codex_ocr_submit_interval_seconds: float = Field(default=5.0, ge=0)
    ocr_timeout_seconds: int = 480
    ocr_languages: list[str] = Field(default_factory=list)


AsrCandidateName = Literal["whisper-existing", "qwen3-asr-1.7b", "qwen3-asr-0.6b", "paraformer-zh", "fun-asr-nano"]


class AsrSettings(AppBaseModel):
    candidate: AsrCandidateName | None = None
    secondary_candidate: AsrCandidateName | None = None
    worker_python: str = ""
    backend_cache_subdir: str = "asr-models"
    max_new_tokens: int = Field(default=2048, ge=1, le=8192)
    chunk_seconds: float = Field(default=30.0, gt=0, le=30)
    terms: list[str] = Field(default_factory=list)
    engine: str
    language: str
    beam_size: int = 5
    vad_filter: bool = False
    condition_on_previous_text: bool = True
    word_timestamps: bool = False
    initial_prompt: str = ""
    model_cache_subdir: str = "faster-whisper"

    @model_validator(mode="after")
    def distinct_candidates(self) -> "AsrSettings":
        if self.secondary_candidate == (self.candidate or "whisper-existing"):
            raise ValueError("第二 ASR 候选必须与主候选不同")
        return self


class SegmentationSettings(AppBaseModel):
    enabled: bool = True
    min_chars_per_block: int = 60
    max_chars_per_block: int = 500
    max_seconds_per_block: float = 30.0
    split_on_empty_line: bool = True
    merge_short_lines: bool = True


class AlignmentSettings(AppBaseModel):
    method: str = "rapidfuzz_ratio"
    top_k: int = 3
    matched_threshold: float = 80.0
    weak_match_threshold: float = 55.0
    use_normalization: bool = True


class ClassificationSettings(AppBaseModel):
    enabled: bool = True
    allow_types: list[str] = Field(default_factory=lambda: ["quote", "lecture", "qa"])
    default_type: str = "lecture"
    qa_section_title: str = "提问环节"
    enable_intro_candidate: bool = True
    quote_score_threshold: float = 85.0
    quote_like_min_score: float = 40.0
    mixed_score_threshold: float = 60.0
    quote_margin_threshold: float = 8.0
    reference_focus_margin: float = 4.0
    qa_keywords: list[str] = Field(
        default_factory=lambda: ["为什么", "怎么", "请问", "是不是", "有没有", "能不能", "如何", "哪一个"]
    )
    intro_keywords: list[str] = Field(
        default_factory=lambda: ["现在播送", "中央人民广播电台", "下面播送", "标题", "作者", "今天我们", "今天继续"]
    )
    lecture_markers: list[str] = Field(
        default_factory=lambda: ["就是说", "我们看", "你看", "意思是", "说明", "比如", "所以", "这个地方", "这里"]
    )


class LLMSettings(AppBaseModel):
    enabled: bool = True
    provider: str = "local_cli"
    model: str = "gpt-6.1-sol"
    gemini_model: str = "Gemini 3.1 Pro (High)"
    gemini_fallback_model: str = ""
    backends: list[str] = Field(default_factory=lambda: ["codex_api"])
    enable_fallback: bool = True
    block_batch_size: int = 2
    block_concurrency: int = 6
    prompt_style: str = "web_like"
    top_matches_for_prompt: int = 3
    max_asr_chars_for_prompt: int = 120
    max_reference_chars_for_prompt: int = 120
    reasoning_effort: str = "high"
    fast_mode: bool = False
    temperature: float = 0.1
    max_output_tokens: int = 4000
    # 对谈阶段 6 可能需要超过 30 分钟完成整篇输出，默认允许等待 60 分钟。
    timeout_seconds: int = 3600
    # 用户要求不合格的校对稿自动重新润色；默认仅重试一次，避免同一坏输入无限占用远程任务，可按部署情况调整。
    refinement_validation_retry_count: int = Field(default=1, ge=0)
    safe_replace_min_score: float = 88.0
    safe_replace_min_margin: float = 6.0
    safe_replace_length_ratio_min: float = 0.8
    safe_replace_length_ratio_max: float = 1.2
    safe_replace_max_extra_content_ratio: float = 0.12
    safe_replace_min_run_length: int = 2


class CodexLBSettings(AppBaseModel):
    # 保留旧配置名称；CPA / OpenAI 兼容网关默认统一走标准 Responses API。
    base_url: str = "http://127.0.0.1:8317"
    base_url_env: str = "CODEX_LB_BASE_URL"
    api_key_env: str = "CODEX_LB_API_KEY"
    responses_path: str = "/v1/responses"
    codex_responses_path: str = "/v1/responses"
    files_create_path: str = "/backend-api/files"
    files_finalize_path_template: str = "/backend-api/files/{file_id}/uploaded"


class OutputSettings(AppBaseModel):
    write_review_json: bool = True
    write_final_markdown: bool = True
    final_markdown_filename: str = "final.md"
    review_json_filename: str = "review.json"
    include_timestamps_in_final: bool = False
    include_reference_in_final: bool = False
    include_notes_in_final: bool = False


class PipelineSettings(AppBaseModel):
    stop_on_error: bool = True
    stages: list[str] = Field(default_factory=list)


class AppSettings(AppBaseModel):
    project: ProjectSettings
    runtime: RuntimeSettings
    profiles: dict[str, ProfileSettings]
    paths: PathsSettings
    audio: AudioSettings
    asr: AsrSettings
    reference: ReferenceSettings
    segmentation: SegmentationSettings
    alignment: AlignmentSettings
    classification: ClassificationSettings
    llm: LLMSettings
    codex_lb: CodexLBSettings = Field(default_factory=CodexLBSettings)
    prompts: PromptSettings | None = None
    output: OutputSettings | None = None
    pipeline: PipelineSettings | None = None


@dataclass(frozen=True)
class LoadedSettings:
    settings: AppSettings
    project_root: Path
    settings_path: Path
    active_profile_name: str
    active_profile: ProfileSettings

    def resolve_path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def path_for(self, field_name: str) -> Path:
        paths = self.settings.paths.model_dump()
        if field_name not in paths:
            raise KeyError(f"未知路径字段: {field_name}")
        return self.resolve_path(paths[field_name])

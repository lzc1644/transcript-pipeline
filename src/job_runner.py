from __future__ import annotations

import json
import logging
import re
import shutil
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

from scripts.run_pipeline import STAGE_EXIT_PARTIAL, run_stage
from src.config_loader import ConfigLoadError, load_settings
from src.glossary_utils import build_initial_prompt, load_glossary_terms, merge_glossary_terms
from src.runtime_utils import ensure_directory, normalize_stage_name, relativize_path, setup_logging
from src.schemas import LoadedSettings
from src.reference_utils import ReferenceFileProgress
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides_to_raw_settings

CANONICAL_INPUT_BASENAME = "source"
WEB_REQUEST_TIMEOUT_SECONDS = 60
CONTENT_TYPE_BOOK_CLUB = "book_club"
CONTENT_TYPE_CONVERSATION = "conversation"
SUPPORTED_CONTENT_TYPES = {CONTENT_TYPE_BOOK_CLUB, CONTENT_TYPE_CONVERSATION}
CONVERSATION_PIPELINE_STAGES = ["extract_audio", "transcribe", "refine", "export_markdown"]


class JobRunnerError(RuntimeError):
    """Raised when a single-run job cannot be prepared or executed."""


@dataclass(frozen=True)
class JobPaths:
    job_id: str
    job_root: Path
    input_videos_dir: Path
    input_reference_dir: Path
    intermediate_audio_dir: Path
    intermediate_asr_dir: Path
    intermediate_ocr_dir: Path
    intermediate_extracted_text_dir: Path
    intermediate_refined_dir: Path
    output_final_dir: Path
    manifest_path: Path
    settings_path: Path


@dataclass(frozen=True)
class JobPreparedInputs:
    video_path: Path
    reference_path: Path | None
    reference_type: str


@dataclass(frozen=True)
class JobResult:
    job_id: str
    job_root: Path
    generated_settings_path: Path
    final_markdown_path: Path
    copied_output_path: Path


@dataclass(frozen=True)
class BatchJobSpec:
    video: str
    reference: str | None
    output_dir: str
    mode: str
    content_type: str = CONTENT_TYPE_BOOK_CLUB
    book_name: str | None = None
    chapter: str | None = None
    glossary_file: str | None = None


@dataclass
class BatchJobRuntime:
    job_id: str
    job_root: Path
    spec: BatchJobSpec
    status: str = "pending"
    current_stage: str = "pending"
    completed_stages: list[str] = field(default_factory=list)
    failed_stage: str | None = None
    error_message: str | None = None
    copied_output_path: Path | None = None
    ocr_items: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchRunSummary:
    batch_id: str
    total: int
    success: int
    failed: int
    partial: int = 0
    items: list[BatchJobRuntime] = field(default_factory=list)


def resolve_common_glossary_path(project_root: Path) -> Path:
    return project_root / "config/glossaries/marxism_common.txt"


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._ignored_tags = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag in {"script", "style", "noscript"}:
            self._ignored_tags += 1
        elif tag in {"p", "br", "div", "article", "section", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_tags > 0:
            self._ignored_tags -= 1
        elif tag in {"p", "div", "article", "section", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_tags:
            return
        stripped = data.strip()
        if stripped:
            self._chunks.append(stripped)

    def get_text(self) -> str:
        raw_text = " ".join(self._chunks)
        normalized = re.sub(r"[ \t]+", " ", raw_text)
        normalized = re.sub(r"\n\s*\n+", "\n\n", normalized)
        return normalized.strip()


def is_url_reference(reference_source: str) -> bool:
    parsed = urlparse(reference_source)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def sanitize_filename_stem(name: str) -> str:
    sanitized = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    return sanitized or CANONICAL_INPUT_BASENAME


def create_job_id() -> str:
    return uuid.uuid4().hex[:12]


def create_batch_id() -> str:
    return uuid.uuid4().hex[:12]


def build_job_paths(project_root: Path, job_id: str) -> JobPaths:
    job_root = ensure_directory(project_root / "data/jobs" / job_id)
    input_dir = ensure_directory(job_root / "input")
    intermediate_dir = ensure_directory(job_root / "intermediate")
    output_dir = ensure_directory(job_root / "output")

    return JobPaths(
        job_id=job_id,
        job_root=job_root,
        input_videos_dir=ensure_directory(input_dir / "videos"),
        input_reference_dir=ensure_directory(input_dir / "reference"),
        intermediate_audio_dir=ensure_directory(intermediate_dir / "audio"),
        intermediate_asr_dir=ensure_directory(intermediate_dir / "asr"),
        intermediate_ocr_dir=ensure_directory(intermediate_dir / "ocr"),
        intermediate_extracted_text_dir=ensure_directory(intermediate_dir / "extracted_text"),
        intermediate_refined_dir=ensure_directory(intermediate_dir / "refined"),
        output_final_dir=ensure_directory(output_dir / "final"),
        manifest_path=job_root / "manifest.json",
        settings_path=job_root / "settings.generated.yaml",
    )


def build_batch_root(project_root: Path, batch_id: str) -> Path:
    return ensure_directory(project_root / "data/jobs/batches" / batch_id)


def supported_video_extensions(loaded_settings: LoadedSettings) -> set[str]:
    return {extension.lower() for extension in loaded_settings.settings.audio.supported_video_ext}


def supported_reference_extensions() -> tuple[str, ...]:
    return (".txt", ".md", ".pdf")


def normalize_content_type(content_type: str | None) -> str:
    normalized = (content_type or CONTENT_TYPE_BOOK_CLUB).strip().lower().replace("-", "_")
    if normalized not in SUPPORTED_CONTENT_TYPES:
        supported = ", ".join(sorted(SUPPORTED_CONTENT_TYPES))
        raise JobRunnerError(f"不支持的 content_type: {content_type}。当前支持: {supported}")
    return normalized


def validate_reference_contract(*, content_type: str, reference: str | None, field_name: str = "reference") -> None:
    normalized_reference = (reference or "").strip()
    if content_type == CONTENT_TYPE_BOOK_CLUB and not normalized_reference:
        raise JobRunnerError(f"读书会模式缺少必填字段: {field_name}")
    if content_type == CONTENT_TYPE_CONVERSATION and normalized_reference:
        raise JobRunnerError(f"对谈模式不使用 {field_name}，请移除参考源或切换 content_type。")


def build_failed_batch_runtime(
    *,
    spec: BatchJobSpec,
    error_message: str,
    failed_stage: str,
    job_id: str = "",
    job_root: Path | None = None,
) -> BatchJobRuntime:
    return BatchJobRuntime(
        job_id=job_id,
        job_root=job_root or Path(),
        spec=spec,
        status="failed",
        current_stage=failed_stage,
        failed_stage=failed_stage,
        error_message=error_message,
    )


def runtime_stage_names(runtime: BatchJobRuntime, project_root: Path) -> tuple[str, ...]:
    loaded_settings = load_settings(
        settings_path=runtime.job_root / "settings.generated.yaml",
        project_root=project_root,
    )
    raw_stages = loaded_settings.settings.pipeline.stages if loaded_settings.settings.pipeline else []
    return tuple(normalize_stage_name(stage_name) for stage_name in raw_stages)


def batch_stage_sequence_for_runtimes(runtimes: list[BatchJobRuntime], project_root: Path) -> list[str]:
    stages: list[str] = []
    for runtime in runtimes:
        if runtime.status == "failed" or not runtime.job_root:
            continue
        settings_path = runtime.job_root / "settings.generated.yaml"
        if not settings_path.exists():
            continue
        for stage_name in runtime_stage_names(runtime, project_root):
            if stage_name not in stages:
                stages.append(stage_name)
    return stages


def remote_pipeline_stages_for_runtime(runtime: BatchJobRuntime, project_root: Path) -> tuple[str, ...]:
    return tuple(
        stage_name
        for stage_name in runtime_stage_names(runtime, project_root)
        if stage_name not in {"extract-audio", "transcribe"}
    )


def resolve_local_path_string(raw_path: str) -> str:
    if not raw_path.strip():
        return ""
    return str(Path(raw_path).expanduser().resolve())


def resolve_batch_job_spec(
    *,
    mode: str,
    video: str,
    reference: str | None,
    output_dir: str,
    book_name: str | None,
    chapter: str | None,
    glossary_file: str | None,
    content_type: str | None = None,
) -> BatchJobSpec:
    normalized_content_type = normalize_content_type(content_type)
    normalized_reference = (reference or "").strip()
    return BatchJobSpec(
        video=resolve_local_path_string(video),
        reference=(
            normalized_reference
            if not normalized_reference or is_url_reference(normalized_reference)
            else resolve_local_path_string(normalized_reference)
        ),
        output_dir=resolve_local_path_string(output_dir),
        mode=mode,
        content_type=normalized_content_type,
        book_name=book_name,
        chapter=chapter,
        glossary_file=resolve_local_path_string(glossary_file or "") or None,
    )


def parse_manifest_jobs(manifest_path: Path) -> list[dict[str, Any]]:
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise JobRunnerError(f"无法读取 manifest: {manifest_path} | {exc}") from exc
    except yaml.YAMLError as exc:
        raise JobRunnerError(f"manifest YAML/JSON 解析失败: {manifest_path} | {exc}") from exc

    if isinstance(payload, list):
        jobs = payload
    elif isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        jobs = payload["jobs"]
    else:
        raise JobRunnerError(f"manifest 结构无效: {manifest_path}，顶层必须是 jobs 列表或包含 jobs 的对象。")

    normalized_jobs: list[dict[str, Any]] = []
    for index, item in enumerate(jobs, start=1):
        if not isinstance(item, dict):
            raise JobRunnerError(f"manifest 第 {index} 条任务无效，必须是对象。")
        normalized_jobs.append(item)
    return normalized_jobs


def validate_manifest_job_entry(
    *,
    item: dict[str, Any],
    default_content_type: str | None,
    default_book_name: str | None,
    default_chapter: str | None,
    default_glossary_file: str | None,
) -> BatchJobSpec:
    allowed_fields = {"video", "reference", "output_dir", "content_type", "book_name", "chapter", "glossary_file"}
    unknown_fields = sorted(set(item) - allowed_fields)
    if unknown_fields:
        raise JobRunnerError(f"包含非法字段: {', '.join(unknown_fields)}")

    content_type = normalize_content_type(str(item.get("content_type") or default_content_type or CONTENT_TYPE_BOOK_CLUB))
    required_fields = ["video", "output_dir"]
    if content_type == CONTENT_TYPE_BOOK_CLUB:
        required_fields.append("reference")
    missing_fields = [field_name for field_name in required_fields if not str(item.get(field_name, "")).strip()]
    if missing_fields:
        raise JobRunnerError(f"缺少必填字段: {', '.join(missing_fields)}")
    validate_reference_contract(
        content_type=content_type,
        reference=str(item.get("reference") or ""),
        field_name="reference",
    )

    return resolve_batch_job_spec(
        mode="manifest",
        video=str(item["video"]),
        reference=str(item.get("reference") or ""),
        output_dir=str(item["output_dir"]),
        content_type=content_type,
        book_name=str(item["book_name"]).strip() if item.get("book_name") else default_book_name,
        chapter=str(item["chapter"]).strip() if item.get("chapter") else default_chapter,
        glossary_file=str(item["glossary_file"]).strip() if item.get("glossary_file") else default_glossary_file,
    )


def find_paired_reference(video_path: Path, reference_dir: Path) -> Path | None:
    matches = [reference_dir / f"{video_path.stem}{extension}" for extension in supported_reference_extensions()]
    existing_matches = [candidate for candidate in matches if candidate.exists()]
    if len(existing_matches) > 1:
        raise JobRunnerError(f"basename={video_path.stem} 匹配到多个 reference 文件。")
    if not existing_matches:
        return None
    return existing_matches[0]


def build_batch_target_path(spec: BatchJobSpec) -> Path:
    return Path(spec.output_dir) / build_final_output_filename(
        Path(spec.video),
        book_name=spec.book_name,
        chapter=spec.chapter,
    )


def collect_duplicate_target_failures(specs: list[BatchJobSpec]) -> tuple[list[BatchJobSpec], list[BatchJobRuntime]]:
    grouped_specs: dict[Path, list[BatchJobSpec]] = {}
    for spec in specs:
        grouped_specs.setdefault(build_batch_target_path(spec), []).append(spec)

    valid_specs: list[BatchJobSpec] = []
    failed_items: list[BatchJobRuntime] = []
    for target_path, grouped in grouped_specs.items():
        if len(grouped) == 1:
            valid_specs.append(grouped[0])
            continue
        for spec in grouped:
            failed_items.append(
                build_failed_batch_runtime(
                    spec=spec,
                    failed_stage="input-validation",
                    error_message=f"重复 target: {target_path}",
                )
            )
    return valid_specs, failed_items


def load_batch_job_specs(
    *,
    base_loaded_settings: LoadedSettings,
    manifest: str | None = None,
    videos_dir: str | None = None,
    reference_dir: str | None = None,
    shared_reference: str | None = None,
    output_dir: str | None = None,
    content_type: str | None = None,
    book_name: str | None = None,
    chapter: str | None = None,
    glossary_file: str | None = None,
) -> tuple[list[BatchJobSpec], list[BatchJobRuntime]]:
    normalized_content_type = normalize_content_type(content_type)
    if normalized_content_type == CONTENT_TYPE_CONVERSATION and (reference_dir or shared_reference):
        raise JobRunnerError("对谈模式不使用 reference_dir 或 shared_reference，请只提供 videos_dir 与 output_dir。")

    input_mode_count = sum(
        [
            1 if manifest else 0,
            1
            if normalized_content_type == CONTENT_TYPE_BOOK_CLUB and videos_dir and reference_dir and output_dir
            else 0,
            1
            if normalized_content_type == CONTENT_TYPE_BOOK_CLUB and videos_dir and shared_reference and output_dir
            else 0,
            1 if normalized_content_type == CONTENT_TYPE_CONVERSATION and videos_dir and output_dir else 0,
        ]
    )
    if input_mode_count != 1:
        raise JobRunnerError("批量入口参数无效：必须且只能选择 manifest、目录配对、共享参考、对谈目录 四种输入模式之一。")

    specs: list[BatchJobSpec] = []
    failed_items: list[BatchJobRuntime] = []

    if manifest:
        manifest_path = Path(manifest).expanduser().resolve()
        for item in parse_manifest_jobs(manifest_path):
            try:
                spec = validate_manifest_job_entry(
                    item=item,
                    default_content_type=normalized_content_type,
                    default_book_name=book_name,
                    default_chapter=chapter,
                    default_glossary_file=glossary_file,
                )
            except JobRunnerError as exc:
                failed_items.append(
                    build_failed_batch_runtime(
                        spec=resolve_batch_job_spec(
                            mode="manifest",
                            video=str(item.get("video", "")),
                            reference=str(item.get("reference") or ""),
                            output_dir=str(item.get("output_dir", "")),
                            content_type=normalized_content_type,
                            book_name=str(item.get("book_name", "")).strip() or book_name,
                            chapter=str(item.get("chapter", "")).strip() or chapter,
                            glossary_file=str(item.get("glossary_file", "")).strip() or glossary_file,
                        ),
                        failed_stage="input-validation",
                        error_message=str(exc),
                    )
                )
                continue
            specs.append(spec)
    else:
        videos_path = Path(videos_dir or "").expanduser().resolve()
        output_path = Path(output_dir or "").expanduser().resolve()
        if not videos_path.exists() or not videos_path.is_dir():
            raise JobRunnerError(f"videos_dir 不存在或不是目录: {videos_path}")

        reference_path = Path(reference_dir).expanduser().resolve() if reference_dir else None
        if reference_path and (not reference_path.exists() or not reference_path.is_dir()):
            raise JobRunnerError(f"reference_dir 不存在或不是目录: {reference_path}")

        allowed_video_extensions = supported_video_extensions(base_loaded_settings)
        for video_path in sorted(videos_path.iterdir()):
            if not video_path.is_file():
                continue
            if video_path.suffix.lower() not in allowed_video_extensions:
                # 批量目录只以受支持的视频文件建任务，允许同目录混放参考文本或 PDF。
                continue

            if reference_path is not None:
                try:
                    matched_reference = find_paired_reference(video_path, reference_path)
                except JobRunnerError as exc:
                    failed_items.append(
                        build_failed_batch_runtime(
                            spec=resolve_batch_job_spec(
                                mode="paired-dir",
                                video=str(video_path),
                                reference="",
                                output_dir=str(output_path),
                                content_type=CONTENT_TYPE_BOOK_CLUB,
                                book_name=book_name,
                                chapter=chapter,
                                glossary_file=glossary_file,
                            ),
                            failed_stage="input-validation",
                            error_message=str(exc),
                        )
                    )
                    continue
                if matched_reference is None:
                    failed_items.append(
                        build_failed_batch_runtime(
                            spec=resolve_batch_job_spec(
                                mode="paired-dir",
                                video=str(video_path),
                                reference="",
                                output_dir=str(output_path),
                                content_type=CONTENT_TYPE_BOOK_CLUB,
                                book_name=book_name,
                                chapter=chapter,
                                glossary_file=glossary_file,
                            ),
                            failed_stage="input-validation",
                            error_message=f"缺少匹配的 reference: {video_path.stem}",
                        )
                    )
                    continue

                specs.append(
                    resolve_batch_job_spec(
                        mode="paired-dir",
                        video=str(video_path),
                        reference=str(matched_reference),
                        output_dir=str(output_path),
                        content_type=CONTENT_TYPE_BOOK_CLUB,
                        book_name=book_name,
                        chapter=chapter,
                        glossary_file=glossary_file,
                    )
                )
                continue

            if normalized_content_type == CONTENT_TYPE_CONVERSATION:
                specs.append(
                    resolve_batch_job_spec(
                        mode="conversation-dir",
                        video=str(video_path),
                        reference=None,
                        output_dir=str(output_path),
                        content_type=CONTENT_TYPE_CONVERSATION,
                        book_name=book_name,
                        chapter=chapter,
                        glossary_file=glossary_file,
                    )
                )
                continue

            if not shared_reference:
                raise JobRunnerError("共享参考模式缺少 shared_reference。")
            specs.append(
                resolve_batch_job_spec(
                    mode="shared-reference",
                    video=str(video_path),
                    reference=shared_reference,
                    output_dir=str(output_path),
                    content_type=CONTENT_TYPE_BOOK_CLUB,
                    book_name=book_name,
                    chapter=chapter,
                    glossary_file=glossary_file,
                )
            )

    valid_specs, duplicate_target_failures = collect_duplicate_target_failures(specs)
    failed_items.extend(duplicate_target_failures)
    return valid_specs, failed_items


def detect_reference_source_type(reference_source: str | Path) -> str:
    candidate = str(reference_source)
    if is_url_reference(candidate):
        return "url"

    path = Path(candidate)
    extension = path.suffix.lower()
    if extension == ".txt":
        return "txt"
    if extension == ".md":
        return "md"
    if extension == ".pdf":
        return "pdf"
    raise JobRunnerError(f"不支持的参考源类型: {reference_source}。当前支持网页链接、txt、md、pdf。")


def copy_local_reference(reference_path: Path, destination_path: Path) -> Path:
    if not reference_path.exists():
        raise JobRunnerError(f"参考源不存在: {reference_path}")
    ensure_directory(destination_path.parent)
    shutil.copy2(reference_path, destination_path)
    return destination_path


def is_network_unreachable_error(exc: OSError) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        error_no = getattr(current, "errno", None)
        if error_no in {101, 65}:
            return True
        if "network is unreachable" in str(current).lower():
            return True
        reason = getattr(current, "reason", None)
        current = reason if isinstance(reason, BaseException) else None
    return False


@contextmanager
def force_ipv4_resolution():
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        resolved = original_getaddrinfo(host, port, family, type, proto, flags)
        ipv4_only = [item for item in resolved if item[0] == socket.AF_INET]
        return ipv4_only or resolved

    socket.getaddrinfo = ipv4_only_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def read_url_payload(
    request: Request,
    *,
    prefer_ipv4: bool = False,
) -> tuple[str, str, bytes]:
    if prefer_ipv4:
        with force_ipv4_resolution():
            with urlopen(request, timeout=WEB_REQUEST_TIMEOUT_SECONDS) as response:
                return (
                    response.headers.get_content_type(),
                    response.headers.get_content_charset() or "utf-8",
                    response.read(),
                )

    with urlopen(request, timeout=WEB_REQUEST_TIMEOUT_SECONDS) as response:
        return (
            response.headers.get_content_type(),
            response.headers.get_content_charset() or "utf-8",
            response.read(),
        )


def fetch_reference_from_url(reference_url: str, destination_dir: Path, basename: str) -> tuple[Path, str]:
    request = Request(reference_url, headers={"User-Agent": "transcript-pipeline/0.1"})
    try:
        content_type, charset, payload = read_url_payload(request)
    except OSError as exc:
        if is_network_unreachable_error(exc):
            try:
                content_type, charset, payload = read_url_payload(request, prefer_ipv4=True)
            except OSError as retry_exc:
                raise JobRunnerError(f"网页参考抓取失败: {reference_url} | {retry_exc}") from retry_exc
        else:
            raise JobRunnerError(f"网页参考抓取失败: {reference_url} | {exc}") from exc

    if content_type == "application/pdf" or reference_url.lower().endswith(".pdf"):
        output_path = destination_dir / f"{basename}.pdf"
        output_path.write_bytes(payload)
        return output_path, "url_pdf"

    text = payload.decode(charset, errors="ignore")
    extractor = HTMLTextExtractor()
    extractor.feed(text)
    extracted_text = extractor.get_text()
    if not extracted_text:
        raise JobRunnerError(f"网页参考提取失败，未得到可用正文: {reference_url}")

    output_path = destination_dir / f"{basename}.txt"
    output_path.write_text(extracted_text, encoding="utf-8")
    return output_path, "url_text"


def prepare_job_inputs(
    *,
    video_source: Path,
    reference_source: str | None,
    job_paths: JobPaths,
    content_type: str | None = None,
) -> JobPreparedInputs:
    normalized_content_type = normalize_content_type(content_type)
    if not video_source.exists():
        raise JobRunnerError(f"视频源不存在: {video_source}")

    video_destination = job_paths.input_videos_dir / f"{CANONICAL_INPUT_BASENAME}{video_source.suffix.lower()}"
    shutil.copy2(video_source, video_destination)

    if normalized_content_type == CONTENT_TYPE_CONVERSATION:
        validate_reference_contract(content_type=normalized_content_type, reference=reference_source)
        return JobPreparedInputs(
            video_path=video_destination,
            reference_path=None,
            reference_type="none",
        )

    validate_reference_contract(content_type=normalized_content_type, reference=reference_source)
    normalized_reference_source = str(reference_source or "").strip()
    reference_type = detect_reference_source_type(normalized_reference_source)
    if reference_type == "url":
        reference_destination, resolved_reference_type = fetch_reference_from_url(
            normalized_reference_source,
            job_paths.input_reference_dir,
            CANONICAL_INPUT_BASENAME,
        )
        return JobPreparedInputs(
            video_path=video_destination,
            reference_path=reference_destination,
            reference_type=resolved_reference_type,
        )

    local_reference_path = Path(normalized_reference_source)
    reference_destination = job_paths.input_reference_dir / f"{CANONICAL_INPUT_BASENAME}{local_reference_path.suffix.lower()}"
    return JobPreparedInputs(
        video_path=video_destination,
        reference_path=copy_local_reference(local_reference_path, reference_destination),
        reference_type=reference_type,
    )


def load_raw_settings(loaded_settings: LoadedSettings) -> dict[str, Any]:
    try:
        with loaded_settings.settings_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except OSError as exc:
        raise JobRunnerError(f"无法读取原始配置文件: {loaded_settings.settings_path} | {exc}") from exc
    except yaml.YAMLError as exc:
        raise JobRunnerError(f"原始配置 YAML 解析失败: {loaded_settings.settings_path} | {exc}") from exc

    if not isinstance(payload, dict):
        raise JobRunnerError(f"原始配置结构无效: {loaded_settings.settings_path}")
    return payload


def build_job_initial_prompt(
    *,
    project_root: Path,
    glossary_file: str | None,
    book_name: str | None,
    chapter: str | None,
    max_chars: int = 400,
) -> str:
    return build_initial_prompt(build_job_asr_terms(project_root=project_root, glossary_file=glossary_file,
                                                   book_name=book_name, chapter=chapter), max_chars=max_chars)


def build_job_asr_terms(*, project_root: Path, glossary_file: str | None,
                        book_name: str | None, chapter: str | None) -> list[str]:
    common = load_glossary_terms(resolve_common_glossary_path(project_root))
    extra = load_glossary_terms(Path(glossary_file).expanduser().resolve()) if glossary_file else []
    return merge_glossary_terms([book_name or "", chapter or ""], extra, common)


def write_job_settings(
    *,
    project_root: Path,
    loaded_settings: LoadedSettings,
    job_paths: JobPaths,
    profile_name: str,
    content_type: str | None = None,
    glossary_file: str | None = None,
    book_name: str | None = None,
    chapter: str | None = None,
    model_overrides: ModelOverrides | None = None,
    refine_prompt: str | None = None,
) -> Path:
    normalized_content_type = normalize_content_type(content_type)
    payload = load_raw_settings(loaded_settings)
    # Freeze the ASR configuration read for this request, including memory
    # overrides; do not reread a later global candidate into a task snapshot.
    payload["asr"] = loaded_settings.settings.asr.model_dump()
    try:
        apply_model_overrides_to_raw_settings(payload, model_overrides or ModelOverrides())
    except SettingsOverrideError as exc:
        raise JobRunnerError(str(exc)) from exc
    payload.setdefault("runtime", {})
    payload["runtime"]["profile"] = profile_name
    payload.setdefault("asr", {})
    payload["asr"]["initial_prompt"] = build_job_initial_prompt(
        project_root=project_root,
        glossary_file=glossary_file,
        book_name=book_name,
        chapter=chapter,
    )
    payload["asr"]["terms"] = merge_glossary_terms(
        build_job_asr_terms(project_root=project_root, glossary_file=glossary_file,
                            book_name=book_name, chapter=chapter), payload["asr"].get("terms", []))
    if normalized_content_type == CONTENT_TYPE_CONVERSATION:
        reference_payload = payload.setdefault("reference", {})
        if not isinstance(reference_payload, dict):
            raise JobRunnerError("配置字段 reference 必须是对象，无法写入对谈模式。")
        reference_payload["enabled"] = False
        pipeline_payload = payload.setdefault("pipeline", {})
        if not isinstance(pipeline_payload, dict):
            raise JobRunnerError("配置字段 pipeline 必须是对象，无法写入对谈模式。")
        pipeline_payload["stages"] = list(CONVERSATION_PIPELINE_STAGES)

    payload["paths"] = {
        "videos_dir": str(job_paths.input_videos_dir),
        "audio_dir": str(job_paths.job_root / "input/audio"),
        "reference_dir": str(job_paths.input_reference_dir),
        "asr_dir": str(job_paths.intermediate_asr_dir),
        "ocr_dir": str(job_paths.intermediate_ocr_dir),
        "extracted_text_dir": str(job_paths.intermediate_extracted_text_dir),
        "chunks_dir": str(job_paths.job_root / "intermediate/chunks"),
        "aligned_dir": str(job_paths.job_root / "intermediate/aligned"),
        "classified_dir": str(job_paths.job_root / "intermediate/classified"),
        "refined_dir": str(job_paths.intermediate_refined_dir),
        "review_dir": str(job_paths.job_root / "output/review"),
        "final_dir": str(job_paths.output_final_dir),
        "logs_dir": str(job_paths.job_root / "output/logs"),
    }

    prompts = payload.get("prompts", {})
    if isinstance(prompts, dict):
        for key, value in list(prompts.items()):
            prompts[key] = str(loaded_settings.resolve_path(str(value)))
        normalized_refine_prompt = (refine_prompt or "").strip()
        if normalized_content_type == CONTENT_TYPE_CONVERSATION and not normalized_refine_prompt:
            conversation_prompt = str(prompts.get("conversation_cleanup") or "").strip()
            if not conversation_prompt:
                raise JobRunnerError("对谈模式缺少阶段 6 提示词配置: prompts.conversation_cleanup")
            prompts["final_cleanup"] = conversation_prompt
        if normalized_refine_prompt:
            prompt_dir = job_paths.job_root / "config/prompts"
            custom_prompt_path = prompt_dir / "final_cleanup.md"
            try:
                prompt_dir.mkdir(parents=True, exist_ok=True)
                custom_prompt_path.write_text(f"{normalized_refine_prompt}\n", encoding="utf-8")
            except OSError as exc:
                raise JobRunnerError(f"无法写入阶段 6 自定义指令文件: {custom_prompt_path} | {exc}") from exc
            prompts["final_cleanup"] = str(custom_prompt_path)
        payload["prompts"] = prompts
    elif normalized_content_type == CONTENT_TYPE_CONVERSATION and not (refine_prompt or "").strip():
        raise JobRunnerError("对谈模式缺少阶段 6 提示词配置: prompts.conversation_cleanup")

    try:
        job_paths.settings_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise JobRunnerError(f"无法写入 job 配置文件: {job_paths.settings_path} | {exc}") from exc

    return job_paths.settings_path


def write_job_manifest(
    *,
    loaded_settings: LoadedSettings,
    job_paths: JobPaths,
    prepared_inputs: JobPreparedInputs,
    video_source: Path,
    reference_source: str | None,
    output_dir: Path,
    profile_name: str,
    content_type: str | None,
    book_name: str | None,
    chapter: str | None,
    glossary_file: str | None,
) -> None:
    normalized_content_type = normalize_content_type(content_type)
    payload = {
        "job_id": job_paths.job_id,
        "profile": profile_name,
        "content_type": normalized_content_type,
        "video_source": str(video_source.resolve()),
        "reference_source": reference_source or "",
        "reference_type": prepared_inputs.reference_type,
        "prepared_video": relativize_path(prepared_inputs.video_path, loaded_settings.project_root),
        "prepared_reference": (
            relativize_path(prepared_inputs.reference_path, loaded_settings.project_root)
            if prepared_inputs.reference_path is not None
            else None
        ),
        "output_dir": str(output_dir.resolve()),
        "book_name": book_name or "",
        "chapter": chapter or "",
        "glossary_file": str(Path(glossary_file).expanduser().resolve()) if glossary_file else "",
        "generated_settings_path": relativize_path(job_paths.settings_path, loaded_settings.project_root),
    }
    snapshot = load_settings(settings_path=job_paths.settings_path, project_root=loaded_settings.project_root)
    payload["asr_candidate"] = snapshot.settings.asr.candidate or "whisper-existing"
    payload["secondary_asr_candidate"] = snapshot.settings.asr.secondary_candidate or ""
    job_paths.manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_final_output_filename(video_source: Path, *, book_name: str | None, chapter: str | None) -> str:
    # 批量上传文件常带有“序号-哈希-”的存储前缀；交付文件必须与视频章节题名一致。
    _ = book_name, chapter
    video_title = re.sub(r"^\d+-[0-9a-fA-F]{8,64}-", "", video_source.stem)
    return f"{sanitize_filename_stem(video_title)}.md"


def copy_final_output(job_paths: JobPaths, output_dir: Path, final_filename: str) -> tuple[Path, Path]:
    final_candidates = sorted(job_paths.output_final_dir.glob("*.md"))
    if not final_candidates:
        raise JobRunnerError(f"job 未生成最终 Markdown 输出: {job_paths.output_final_dir}")

    source_path = final_candidates[0]
    ensure_directory(output_dir)
    destination_path = output_dir / final_filename
    shutil.copy2(source_path, destination_path)
    text_source_path = source_path.with_suffix(".txt")
    if text_source_path.is_file():
        shutil.copy2(text_source_path, destination_path.with_suffix(".txt"))
    return source_path, destination_path


def prepare_batch_jobs(
    *,
    project_root: Path,
    base_loaded_settings: LoadedSettings,
    job_specs: list[BatchJobSpec],
    model_overrides: ModelOverrides | None = None,
    refine_prompt: str | None = None,
) -> list[BatchJobRuntime]:
    runtimes: list[BatchJobRuntime] = []
    profile_name = base_loaded_settings.active_profile_name

    for spec in job_specs:
        job_id = create_job_id()
        job_paths = build_job_paths(project_root, job_id)
        try:
            prepared_inputs = prepare_job_inputs(
                video_source=Path(spec.video),
                reference_source=spec.reference,
                job_paths=job_paths,
                content_type=spec.content_type,
            )
            write_job_settings(
                project_root=project_root,
                loaded_settings=base_loaded_settings,
                job_paths=job_paths,
                profile_name=profile_name,
                content_type=spec.content_type,
                glossary_file=spec.glossary_file,
                book_name=spec.book_name,
                chapter=spec.chapter,
                model_overrides=model_overrides,
                refine_prompt=refine_prompt,
            )
            write_job_manifest(
                loaded_settings=base_loaded_settings,
                job_paths=job_paths,
                prepared_inputs=prepared_inputs,
                video_source=Path(spec.video),
                reference_source=spec.reference,
                output_dir=Path(spec.output_dir),
                profile_name=profile_name,
                content_type=spec.content_type,
                book_name=spec.book_name,
                chapter=spec.chapter,
                glossary_file=spec.glossary_file,
            )
        except JobRunnerError as exc:
            runtimes.append(
                build_failed_batch_runtime(
                    spec=spec,
                    job_id=job_id,
                    job_root=job_paths.job_root,
                    failed_stage="prepare-job",
                    error_message=str(exc),
                )
            )
            continue

        runtimes.append(
            BatchJobRuntime(
                job_id=job_id,
                job_root=job_paths.job_root,
                spec=spec,
                status="pending",
            )
        )

    return runtimes


def execute_batch_stage_for_runtime(
    *,
    stage_name: str,
    runtime: BatchJobRuntime,
    project_root: Path,
    logger: logging.Logger,
    backend_override: str | None = None,
    progress_callback: Callable[[BatchJobRuntime, ReferenceFileProgress], None] | None = None,
    stage_callback: Callable[[BatchJobRuntime], None] | None = None,
) -> None:
    if runtime.status in {"failed", "partial"}:
        return

    runtime.status = "running"
    runtime.current_stage = stage_name
    if stage_callback is not None:
        stage_callback(runtime)

    try:
        job_loaded_settings = load_settings(
            settings_path=runtime.job_root / "settings.generated.yaml",
            project_root=project_root,
        )
        current_backend_override = backend_override if stage_name == "refine" else None
        reference_progress_callback = (
            (lambda progress: progress_callback(runtime, progress))
            if stage_name == "prepare-reference" and progress_callback is not None
            else None
        )
        exit_code = run_stage(
            stage_name,
            job_loaded_settings,
            logger,
            backend_override=current_backend_override,
            prepare_reference_progress_callback=reference_progress_callback,
        )
        if exit_code == STAGE_EXIT_PARTIAL:
            runtime.status = "partial"
            runtime.failed_stage = stage_name
            runtime.error_message = "准备参考 OCR 尚有缺页，请重试缺失页。"
            return
        if exit_code != 0:
            raise JobRunnerError(f"job 主链失败: stage={stage_name} exit_code={exit_code}")

        if stage_name not in runtime.completed_stages:
            runtime.completed_stages.append(stage_name)
        if stage_name == "export-markdown":
            job_paths = build_job_paths(project_root, runtime.job_id)
            _, copied_output_path = copy_final_output(
                job_paths,
                Path(runtime.spec.output_dir),
                build_final_output_filename(
                    Path(runtime.spec.video),
                    book_name=runtime.spec.book_name,
                    chapter=runtime.spec.chapter,
                ),
            )
            runtime.copied_output_path = copied_output_path
            runtime.status = "success"
            runtime.current_stage = "done"
    except (ConfigLoadError, JobRunnerError) as exc:
        runtime.status = "failed"
        runtime.failed_stage = stage_name
        runtime.error_message = str(exc)
    finally:
        if stage_callback is not None:
            stage_callback(runtime)


def run_jobs_with_limited_concurrency(
    *,
    stage_name: str,
    runtimes: list[BatchJobRuntime],
    project_root: Path,
    logger: logging.Logger,
    remote_concurrency: int,
    backend_override: str | None = None,
    progress_callback: Callable[[BatchJobRuntime, ReferenceFileProgress], None] | None = None,
) -> None:
    if not runtimes:
        return

    max_workers = max(1, remote_concurrency)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                execute_batch_stage_for_runtime,
                stage_name=stage_name,
                runtime=runtime,
                project_root=project_root,
                logger=logger,
                backend_override=backend_override,
                progress_callback=progress_callback,
            )
            for runtime in runtimes
        ]
        for future in as_completed(futures):
            future.result()


def execute_remote_pipeline_for_runtime(
    *,
    runtime: BatchJobRuntime,
    project_root: Path,
    logger: logging.Logger,
    backend_override: str | None = None,
    progress_callback: Callable[[BatchJobRuntime, ReferenceFileProgress], None] | None = None,
    stage_callback: Callable[[BatchJobRuntime], None] | None = None,
) -> None:
    for stage_name in remote_pipeline_stages_for_runtime(runtime, project_root):
        execute_batch_stage_for_runtime(
            stage_name=stage_name,
            runtime=runtime,
            project_root=project_root,
            logger=logger,
            backend_override=backend_override,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
        if runtime.status in {"failed", "partial"}:
            return


def run_batch_stage(
    *,
    stage_name: str,
    runtimes: list[BatchJobRuntime],
    project_root: Path,
    logger: logging.Logger,
    remote_concurrency: int,
    backend_override: str | None = None,
    progress_callback: Callable[[BatchJobRuntime, ReferenceFileProgress], None] | None = None,
) -> None:
    active_runtimes = [
        runtime
        for runtime in runtimes
        if runtime.status not in {"failed", "partial"} and stage_name in runtime_stage_names(runtime, project_root)
    ]
    if not active_runtimes:
        return

    if stage_name in {"prepare-reference", "refine"}:
        run_jobs_with_limited_concurrency(
            stage_name=stage_name,
            runtimes=active_runtimes,
            project_root=project_root,
            logger=logger,
            remote_concurrency=remote_concurrency,
            backend_override=backend_override,
            progress_callback=progress_callback,
        )
        return

    for runtime in active_runtimes:
        execute_batch_stage_for_runtime(
            stage_name=stage_name,
            runtime=runtime,
            project_root=project_root,
            logger=logger,
            backend_override=backend_override,
            progress_callback=progress_callback,
        )


def serialize_batch_runtime(runtime: BatchJobRuntime) -> dict[str, Any]:
    return {
        "job_id": runtime.job_id,
        "mode": runtime.spec.mode,
        "content_type": runtime.spec.content_type,
        "video_source": runtime.spec.video,
        "reference_source": runtime.spec.reference or "",
        "output_dir": runtime.spec.output_dir,
        "book_name": runtime.spec.book_name or "",
        "chapter": runtime.spec.chapter or "",
        "glossary_file": runtime.spec.glossary_file or "",
        "status": runtime.status,
        "current_stage": runtime.current_stage,
        "completed_stages": list(runtime.completed_stages),
        "failed_stage": runtime.failed_stage or "",
        "error_message": runtime.error_message or "",
        "copied_output_path": str(runtime.copied_output_path) if runtime.copied_output_path else "",
        "ocr_items": [runtime.ocr_items[key] for key in sorted(runtime.ocr_items)],
        "pages_total": sum(int(item.get("page_count") or 0) for item in runtime.ocr_items.values()),
        "pages_completed": sum(int(item.get("completed_pages") or 0) for item in runtime.ocr_items.values()),
        "pages_failed": sum(len(item.get("failed_page_numbers") or []) for item in runtime.ocr_items.values()),
        "resumable": runtime.status == "partial",
    }


def write_batch_summary(
    *,
    project_root: Path,
    summary: BatchRunSummary,
) -> None:
    batch_root = build_batch_root(project_root, summary.batch_id)
    manifest_payload = {
        "batch_id": summary.batch_id,
        "total": summary.total,
        "items": [
            {
                "job_id": item.job_id,
                "mode": item.spec.mode,
                "content_type": item.spec.content_type,
                "video_source": item.spec.video,
                "reference_source": item.spec.reference or "",
                "output_dir": item.spec.output_dir,
                "book_name": item.spec.book_name or "",
                "chapter": item.spec.chapter or "",
                "glossary_file": item.spec.glossary_file or "",
            }
            for item in summary.items
        ],
    }
    (batch_root / "manifest.json").write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_payload = {
        "batch_id": summary.batch_id,
        "total": summary.total,
        "success": summary.success,
        "failed": summary.failed,
        "partial": summary.partial,
        "items": [serialize_batch_runtime(item) for item in summary.items],
    }
    (batch_root / "summary.json").write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# Batch Summary: {summary.batch_id}",
        "",
        f"- total: {summary.total}",
        f"- success: {summary.success}",
        f"- failed: {summary.failed}",
        f"- partial: {summary.partial}",
        "",
    ]
    for item in summary.items:
        lines.append(f"## {item.job_id or 'invalid'}")
        lines.append(f"- status: {item.status}")
        lines.append(f"- mode: {item.spec.mode}")
        lines.append(f"- content_type: {item.spec.content_type}")
        lines.append(f"- video: {item.spec.video}")
        lines.append(f"- reference: {item.spec.reference or '无'}")
        lines.append(f"- failed_stage: {item.failed_stage or ''}")
        lines.append(f"- copied_output_path: {item.copied_output_path or ''}")
        lines.append(f"- error_message: {item.error_message or ''}")
        lines.append("")
    (batch_root / "summary.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def run_batch_jobs(
    *,
    project_root: Path,
    base_loaded_settings: LoadedSettings,
    job_specs: list[BatchJobSpec],
    failed_runtimes: list[BatchJobRuntime] | None = None,
    remote_concurrency: int = 2,
    batch_id: str | None = None,
    backend_override: str | None = None,
    model_overrides: ModelOverrides | None = None,
) -> BatchRunSummary:
    current_batch_id = batch_id or create_batch_id()
    logger = setup_logging(base_loaded_settings.settings.runtime.log_level)
    runtimes = list(failed_runtimes or [])
    runtimes.extend(
        prepare_batch_jobs(
            project_root=project_root,
            base_loaded_settings=base_loaded_settings,
            job_specs=job_specs,
            model_overrides=model_overrides,
        )
    )

    active_runtimes = [runtime for runtime in runtimes if runtime.status not in {"failed", "partial"}]
    max_workers = max(1, remote_concurrency)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        remote_futures = []
        for runtime in active_runtimes:
            for stage_name in ("extract-audio", "transcribe"):
                execute_batch_stage_for_runtime(
                    stage_name=stage_name,
                    runtime=runtime,
                    project_root=project_root,
                    logger=logger,
                    backend_override=backend_override,
                )
                if runtime.status in {"failed", "partial"}:
                    break

            if runtime.status in {"failed", "partial"}:
                continue

            remote_futures.append(
                executor.submit(
                    execute_remote_pipeline_for_runtime,
                    runtime=runtime,
                    project_root=project_root,
                    logger=logger,
                    backend_override=backend_override,
                )
            )

        for future in as_completed(remote_futures):
            future.result()

    success_count = sum(1 for item in runtimes if item.status == "success")
    failed_count = sum(1 for item in runtimes if item.status == "failed")
    partial_count = sum(1 for item in runtimes if item.status == "partial")
    summary = BatchRunSummary(
        batch_id=current_batch_id,
        total=len(runtimes),
        success=success_count,
        failed=failed_count,
        partial=partial_count,
        items=runtimes,
    )
    write_batch_summary(project_root=project_root, summary=summary)
    return summary


def get_batch_exit_code(summary: BatchRunSummary) -> int:
    if summary.success == summary.total and summary.total > 0:
        return 0
    if summary.partial > 0 or summary.success > 0:
        return 2
    return 1


def run_job_pipeline(
    loaded_settings: LoadedSettings,
    logger: logging.Logger,
    backend_override: str | None = None,
) -> None:
    raw_stages = loaded_settings.settings.pipeline.stages if loaded_settings.settings.pipeline else []
    if not raw_stages:
        raise JobRunnerError("当前配置未定义主链阶段列表。")

    stages = [normalize_stage_name(stage_name) for stage_name in raw_stages]
    logger.info("job 主链启动 | profile=%s | stages=%s", loaded_settings.active_profile_name, ",".join(stages))
    for stage_name in stages:
        logger.info("job 主链执行 | stage=%s", stage_name)
        current_backend_override = backend_override if stage_name == "refine" else None
        exit_code = run_stage(stage_name, loaded_settings, logger, backend_override=current_backend_override)
        if exit_code != 0:
            raise JobRunnerError(f"job 主链失败: stage={stage_name} exit_code={exit_code}")


def run_single_job(
    *,
    project_root: Path,
    base_loaded_settings: LoadedSettings,
    video: str,
    reference: str | None,
    output_dir: str,
    content_type: str | None = None,
    profile: str | None = None,
    backend: str | None = None,
    asr_candidate: str | None = None,
    secondary_asr_candidate: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    ocr_model: str | None = None,
    ocr_reasoning_effort: str | None = None,
    ocr_max_concurrency: int | None = None,
    ocr_submit_interval_seconds: float | None = None,
    book_name: str | None = None,
    chapter: str | None = None,
    glossary_file: str | None = None,
    refine_prompt: str | None = None,
) -> JobResult:
    normalized_content_type = normalize_content_type(content_type)
    video_source = Path(video).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()
    profile_name = profile or base_loaded_settings.active_profile_name
    job_id = create_job_id()
    job_paths = build_job_paths(project_root, job_id)
    prepared_inputs = prepare_job_inputs(
        video_source=video_source,
        reference_source=reference,
        job_paths=job_paths,
        content_type=normalized_content_type,
    )

    generated_settings_path = write_job_settings(
        project_root=project_root,
        loaded_settings=base_loaded_settings,
        job_paths=job_paths,
        profile_name=profile_name,
        content_type=normalized_content_type,
        glossary_file=glossary_file,
        book_name=book_name,
        chapter=chapter,
        model_overrides=ModelOverrides(
            asr_candidate=asr_candidate,
            secondary_asr_candidate=secondary_asr_candidate,
            llm_model=model,
            llm_reasoning_effort=reasoning_effort,
            ocr_model=ocr_model,
            ocr_reasoning_effort=ocr_reasoning_effort,
            ocr_max_concurrency=ocr_max_concurrency,
            ocr_submit_interval_seconds=ocr_submit_interval_seconds,
        ),
        refine_prompt=refine_prompt,
    )
    write_job_manifest(
        loaded_settings=base_loaded_settings,
        job_paths=job_paths,
        prepared_inputs=prepared_inputs,
        video_source=video_source,
        reference_source=reference,
        output_dir=output_path,
        profile_name=profile_name,
        content_type=normalized_content_type,
        book_name=book_name,
        chapter=chapter,
        glossary_file=glossary_file,
    )

    try:
        job_loaded_settings = load_settings(
            settings_path=generated_settings_path,
            profile_name=profile_name,
            project_root=project_root,
        )
    except ConfigLoadError as exc:
        raise JobRunnerError(f"job 配置加载失败: {generated_settings_path} | {exc}") from exc

    logger = setup_logging(job_loaded_settings.settings.runtime.log_level)
    logger.info(
        "job 启动 | job_id=%s | content_type=%s | reference_type=%s",
        job_id,
        normalized_content_type,
        prepared_inputs.reference_type,
    )
    run_job_pipeline(job_loaded_settings, logger, backend_override=backend)

    final_source_path, copied_output_path = copy_final_output(
        job_paths,
        output_path,
        build_final_output_filename(video_source, book_name=book_name, chapter=chapter),
    )
    logger.info("job 完成 | job_id=%s | final=%s", job_id, copied_output_path)

    return JobResult(
        job_id=job_id,
        job_root=job_paths.job_root,
        generated_settings_path=generated_settings_path,
        final_markdown_path=final_source_path,
        copied_output_path=copied_output_path,
    )

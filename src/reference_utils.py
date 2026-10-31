from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from src.codex_lb_client import CodexLBClient
from src.llm.gateway import connection_from_settings
from src.pdf.errors import (
    ReferencePreparationError, ReferenceInputEmptyError, PdfDependencyError,
    AgyOCRError, CodexOCRError, CodexOCRPagesIncompleteError,
)
from src.pdf.ocr import (
    PDFOCROptions, PDFOCRRuntime, ocr_pdf, META_LINE_MARKERS,
    strip_fenced_text, is_page_marker_line, is_page_number_line, is_meta_line,
    sanitize_gemini_ocr_text, build_codex_api_image_ocr_prompt,
    build_pdf_book_ocr_image_prompt, get_pdf_page_count,
    render_pdf_page_as_png_data_url, codex_ocr_page_checkpoint_path,
    load_codex_ocr_page_checkpoints, write_codex_ocr_page_checkpoint,
    load_codex_ocr_page_sources, write_codex_ocr_page_source,
)
from src.ocr_page_markers import format_ocr_page_break
from src.ocr_scheduler import OCRPageTask, run_staggered_page_ocr_tasks
from src.pdf_ocr_workflow import (
    PDFOCRPageModel,
    PDFOCRPageState,
    PDFOCRRetryPlan,
    build_pdf_ocr_checkpoint_namespace,
    build_pdf_ocr_run_identity,
    select_pdf_ocr_page_model,
)
from src.runtime_utils import ensure_directory
from src.schemas import LoadedSettings

MIN_EXTRACTED_PDF_TEXT_LENGTH = 10
AI_OCR_BACKEND_CODEX_API = "codex_api"
AI_OCR_BACKEND_CODEX_CLI = "codex_cli"
AI_OCR_BACKEND_AGY = "agy"
VALID_AI_OCR_BACKENDS = (AI_OCR_BACKEND_CODEX_API, AI_OCR_BACKEND_AGY, AI_OCR_BACKEND_CODEX_CLI)
OCR_SECTION_LABEL_SUFFIXES = ("段", "章", "节", "篇", "部分", "附录")
LOGGER = logging.getLogger(__name__)


CodexOCRPageProgress = PDFOCRPageState


@dataclass(frozen=True)
class ReferenceOutputPaths:
    txt_path: Path
    json_path: Path


@dataclass(frozen=True)
class ReferenceFileResult:
    source_file: str
    source_type: str
    output_text_file: str
    extraction_method: str
    success: bool
    text_length: int
    warnings: list[str]
    extracted_text: str
    page_count: int = 0
    completed_pages: int = 0
    failed_page_numbers: tuple[int, ...] = ()
    page_errors: dict[int, str] = field(default_factory=dict)
    resumable: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ReferenceFileProgress:
    source_path: Path
    output_text_path: Path
    page_count: int
    completed_pages: int
    failed_page_numbers: tuple[int, ...]
    page_errors: dict[int, str]
    resumable: bool
    status: str = "running"


@dataclass(frozen=True)
class ReferenceBatchItem:
    source_path: Path
    output_paths: ReferenceOutputPaths
    success: bool
    warnings: list[str]
    error: str | None = None
    page_count: int = 0
    completed_pages: int = 0
    failed_page_numbers: tuple[int, ...] = ()
    page_errors: dict[int, str] = field(default_factory=dict)
    resumable: bool = False


@dataclass(frozen=True)
class ReferenceBatchSummary:
    total: int
    success: int
    skipped: int
    failed: int
    items: list[ReferenceBatchItem]

    @property
    def partial(self) -> int:
        return sum(item.resumable for item in self.items)


def normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def get_supported_reference_extensions(loaded_settings: LoadedSettings) -> list[str]:
    settings = loaded_settings.settings.reference
    extensions: list[str] = []
    if settings.allow_txt:
        extensions.append(".txt")
    if settings.allow_md:
        extensions.append(".md")
    if settings.allow_pdf:
        extensions.append(".pdf")
    return extensions


def iter_reference_files(reference_dir: Path, allowed_extensions: Iterable[str]) -> list[Path]:
    normalized_extensions = {normalize_extension(extension) for extension in allowed_extensions}
    if not reference_dir.exists():
        return []

    return sorted(
        path
        for path in reference_dir.iterdir()
        if path.is_file() and path.suffix.lower() in normalized_extensions
    )


def build_reference_output_paths(reference_path: Path, output_dir: Path) -> ReferenceOutputPaths:
    return ReferenceOutputPaths(
        txt_path=output_dir / f"{reference_path.stem}.txt",
        json_path=output_dir / f"{reference_path.stem}.json",
    )


def build_source_file_label(source_path: Path, loaded_settings: LoadedSettings) -> str:
    try:
        return str(source_path.resolve().relative_to(loaded_settings.project_root))
    except ValueError:
        return str(source_path.resolve())


def read_text_file(reference_path: Path) -> str:
    try:
        return reference_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return reference_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ReferencePreparationError(f"读取文件失败: {reference_path.name} | {exc}") from exc


def is_effectively_empty_text(text: str) -> bool:
    return len("".join(text.split())) < MIN_EXTRACTED_PDF_TEXT_LENGTH


def import_pdf_reader() -> Callable[[str], object]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfDependencyError("未安装 pypdf。请先执行 `pip install -r requirements.txt`。") from exc

    return PdfReader


def extract_pdf_text(reference_path: Path) -> tuple[str, list[str]]:
    PdfReader = import_pdf_reader()
    warnings: list[str] = []

    try:
        reader = PdfReader(str(reference_path))
    except Exception as exc:
        raise ReferencePreparationError(f"PDF 打开失败: {reference_path.name} | {exc}") from exc

    parts: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise ReferencePreparationError(f"PDF 文本提取失败: {reference_path.name} | {exc}") from exc
        if page_text:
            parts.append(page_text)

    text = "\n".join(parts).strip()
    if is_effectively_empty_text(text):
        warnings.append("PDF 提取结果为空或接近空，可能是扫描版 PDF；当前阶段未启用 OCR。")

    return text, warnings


def get_ocr_language_code(loaded_settings: LoadedSettings) -> str:
    languages = [language.strip() for language in loaded_settings.settings.reference.ocr_languages if language.strip()]
    if not languages:
        return "chi_sim+eng"
    return "+".join(languages)


def is_gemini_capacity_error(text: str) -> bool:
    normalized = text.upper()
    return "429" in normalized or "MODEL_CAPACITY_EXHAUSTED" in normalized or "RESOURCE_EXHAUSTED" in normalized


def is_cjk_content_line(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def normalize_ocrmypdf_edge_line(text: str) -> str:
    normalized = re.sub(r"\d+", "#", text.strip())
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def normalize_ocrmypdf_body_line(text: str) -> str:
    normalized = re.sub(r"[ \t]{2,}", " ", text)
    normalized = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[，。；：！？、“”‘’《》（）])", "", normalized)
    normalized = re.sub(r"(?<=[，。；：！？、“”‘’《》（）])\s+(?=[\u3400-\u9fff])", "", normalized)
    tokens = normalized.strip().split(" ")
    if len(tokens) <= 1:
        return normalized.strip()

    merged_tokens: list[str] = [tokens[0]]
    for token in tokens[1:]:
        previous = merged_tokens[-1]
        previous_tail = re.search(r"[\u3400-\u9fff]+$", previous)
        current_head = re.match(r"^[\u3400-\u9fff]+", token)
        should_merge = (
            previous_tail is not None
            and current_head is not None
            and (len(previous_tail.group(0)) == 1 or len(current_head.group(0)) == 1)
            and not previous.endswith(OCR_SECTION_LABEL_SUFFIXES)
        )
        if should_merge:
            merged_tokens[-1] = previous + token
        else:
            merged_tokens.append(token)

    return " ".join(merged_tokens).strip()


def is_likely_ocrmypdf_header_footer(text: str) -> bool:
    stripped = text.strip()
    return (
        len(stripped) <= 40
        and bool(re.search(r"\d", stripped))
        and is_cjk_content_line(stripped)
        and not bool(re.search(r"[。！？；]", stripped))
    )


def is_likely_ocrmypdf_garbled_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    cjk_count = len(re.findall(r"[\u3400-\u9fff]", stripped))
    ascii_alpha_count = len(re.findall(r"[A-Za-z]", stripped))
    digit_count = len(re.findall(r"\d", stripped))
    visible_len = len(re.sub(r"\s+", "", stripped))

    return (
        cjk_count <= 2
        and ascii_alpha_count >= 6
        and ascii_alpha_count + digit_count >= max(8, visible_len // 2)
    )


def is_likely_ocrmypdf_tiny_garbage_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped or is_page_number_line(stripped):
        return False

    visible = re.sub(r"\s+", "", stripped)
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", visible))
    ascii_alpha_count = len(re.findall(r"[A-Za-z]", visible))
    digit_count = len(re.findall(r"\d", visible))
    punctuation_count = len(re.findall(r"[^A-Za-z0-9\u3400-\u9fff]", visible))

    return (
        len(visible) <= 4
        and cjk_count == 0
        and ascii_alpha_count + digit_count + punctuation_count == len(visible)
        and (punctuation_count >= 1 or ascii_alpha_count >= 2)
    )


def should_merge_ocrmypdf_lines(previous: str, current: str) -> bool:
    if not previous or not current:
        return False

    if previous.endswith(("。", "！", "？", "；", "：")):
        return False

    if re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节篇部卷节回讲]", current):
        return False

    previous_has_cjk_tail = bool(re.search(r"[\u3400-\u9fff]$", previous))
    current_has_cjk_head = bool(re.match(r"^[\u3400-\u9fff，。；：！？、“”‘’《》（）]", current))
    return previous_has_cjk_tail and current_has_cjk_head


def merge_ocrmypdf_body_lines(lines: list[str]) -> list[str]:
    merged_lines: list[str] = []
    for line in lines:
        if merged_lines and should_merge_ocrmypdf_lines(merged_lines[-1], line):
            merged_lines[-1] = f"{merged_lines[-1]}{line}"
            continue
        merged_lines.append(line)
    return merged_lines


def sanitize_ocrmypdf_text(text: str) -> str:
    raw_pages = [page for page in text.replace("\r\n", "\n").split("\f")]
    page_lines: list[list[str]] = []

    for raw_page in raw_pages:
        lines = [normalize_ocrmypdf_body_line(line) for line in raw_page.splitlines()]
        lines = [line for line in lines if line]
        if lines:
            page_lines.append(lines)

    edge_counts: dict[str, int] = {}
    for lines in page_lines:
        for candidate in (lines[:1] + lines[-1:]):
            key = normalize_ocrmypdf_edge_line(candidate)
            if key:
                edge_counts[key] = edge_counts.get(key, 0) + 1

    cleaned_pages: list[str] = []
    for lines in page_lines:
        start = 0
        end = len(lines)

        while start < end:
            line = lines[start]
            key = normalize_ocrmypdf_edge_line(line)
            if (
                is_page_number_line(line)
                or edge_counts.get(key, 0) >= 2
                or is_likely_ocrmypdf_header_footer(line)
            ):
                start += 1
                continue
            break

        while end > start:
            line = lines[end - 1]
            key = normalize_ocrmypdf_edge_line(line)
            if (
                is_page_number_line(line)
                or edge_counts.get(key, 0) >= 2
                or is_likely_ocrmypdf_header_footer(line)
            ):
                end -= 1
                continue
            break

        page_body_lines = [
            line
            for line in lines[start:end]
            if not is_page_number_line(line)
            and not is_likely_ocrmypdf_garbled_line(line)
            and not is_likely_ocrmypdf_tiny_garbage_line(line)
        ]
        page_body_lines = merge_ocrmypdf_body_lines(page_body_lines)
        cleaned_page = "\n".join(page_body_lines).strip()
        if cleaned_page:
            cleaned_pages.append(cleaned_page)

    return "\n\n".join(cleaned_pages).strip()


def build_gemini_ocr_prompt(reference_path: str | Path) -> str:
    return "\n".join(
        [
            "你现在要对一份中文 PDF 做 OCR 提取。",
            "任务要求：",
            "1. 只输出提取后的纯文本。",
            "2. 不要解释，不要总结，不要添加说明。",
            "3. 保留原文顺序，尽量保留自然段。",
            "4. 不要输出 Markdown，不要输出 JSON。",
            "5. 不要输出页码、分页标记、页眉、页尾、Page 1 之类的分页提示。",
            "",
            f"请处理这个 PDF 文件：@{{{reference_path}}}",
        ]
    )


def build_codex_ocr_prompt(reference_path: str | Path) -> str:
    return "\n".join(
        [
            "你现在要对一份中文 PDF 做 OCR 提取。",
            "任务要求：",
            "1. 只输出提取后的纯文本。",
            "2. 不要解释，不要总结，不要添加说明。",
            "3. 保留原文顺序，尽量保留自然段。",
            "4. 不要输出 Markdown，不要输出 JSON。",
            "5. 不要输出页码、分页标记、页眉、页尾、Page 1 之类的分页提示。",
            "6. 强制要求：禁止调用本机的 OCR 工具、外部命令、脚本或系统程序。",
            "7. 强制要求：只使用模型自身的视觉能力直接阅读这个 PDF。",
            "",
            f"请处理这个 PDF 文件：@{{{reference_path}}}",
        ]
    )


def describe_ai_ocr_backend(backend: str) -> str:
    if backend == AI_OCR_BACKEND_CODEX_API:
        return "Codex API"
    if backend == AI_OCR_BACKEND_CODEX_CLI:
        return "Codex CLI"
    if backend == AI_OCR_BACKEND_AGY:
        return "agy"
    return backend


def ai_ocr_method_name(backend: str) -> str:
    if backend == AI_OCR_BACKEND_CODEX_API:
        return "codex_api_pdf_ocr"
    if backend == AI_OCR_BACKEND_CODEX_CLI:
        return "codex_cli_pdf_ocr"
    if backend == AI_OCR_BACKEND_AGY:
        return "agy_pdf_ocr"
    raise ReferencePreparationError(f"未知 PDF AI OCR 后端: {backend}")


def build_ai_ocr_fallback_backends(primary_backend: str) -> list[str]:
    return [backend for backend in VALID_AI_OCR_BACKENDS if backend != primary_backend]


def build_agy_ocr_workspace(reference_path: Path, loaded_settings: LoadedSettings) -> tuple[Path, Path]:
    ocr_dir = ensure_directory(loaded_settings.path_for("ocr_dir"))
    source_fingerprint = hashlib.sha1(str(reference_path.resolve()).encode("utf-8")).hexdigest()[:10]
    workspace_dir = ensure_directory(ocr_dir / "agy_workspace" / f"{reference_path.stem}-{source_fingerprint}")
    staged_pdf_path = workspace_dir / reference_path.name
    if staged_pdf_path.resolve() != reference_path.resolve():
        shutil.copy2(reference_path, staged_pdf_path)
    return workspace_dir, staged_pdf_path


def build_codex_ocr_workspace(reference_path: Path, loaded_settings: LoadedSettings) -> tuple[Path, Path]:
    ocr_dir = ensure_directory(loaded_settings.path_for("ocr_dir"))
    source_fingerprint = hashlib.sha1(str(reference_path.resolve()).encode("utf-8")).hexdigest()[:10]
    workspace_dir = ensure_directory(ocr_dir / "codex_cli_workspace" / f"{reference_path.stem}-{source_fingerprint}")
    staged_pdf_path = workspace_dir / reference_path.name
    if staged_pdf_path.resolve() != reference_path.resolve():
        shutil.copy2(reference_path, staged_pdf_path)
    return workspace_dir, staged_pdf_path


def run_codex_api_pdf_ocr(
    reference_path: Path,
    loaded_settings: LoadedSettings,
    *,
    sidecar_path: Path | None = None,
    checkpoint_dir: Path | None = None,
    progress_callback: Callable[[CodexOCRPageProgress], None] | None = None,
    prompt_builder: Callable[[str | Path, int, int], str] = build_codex_api_image_ocr_prompt,
    retry_plan: PDFOCRRetryPlan | None = None,
    gateway_connection=None,
) -> tuple[str, list[str]]:
    settings = loaded_settings.settings.reference
    if not settings.codex_ocr_model.strip():
        raise CodexOCRError("codex_api OCR 需要配置 reference.codex_ocr_model。")
    client = CodexLBClient(
        loaded_settings.settings.codex_lb,
        timeout_seconds=settings.ocr_timeout_seconds,
        require_stream_completed=True,
        connection=(gateway_connection if gateway_connection is not None else
                    connection_from_settings(loaded_settings.settings.codex_lb)),
    )
    output_path = Path(sidecar_path) if sidecar_path is not None else (
        loaded_settings.path_for("ocr_dir") / f"{reference_path.stem}.codex_api_ocr.txt"
    )
    result = ocr_pdf(
        reference_path, output_path,
        PDFOCROptions(settings.codex_ocr_model, settings.codex_ocr_reasoning_effort,
                      settings.codex_ocr_fast_mode, settings.codex_ocr_max_concurrency,
                      settings.codex_ocr_submit_interval_seconds),
        gateway=client, checkpoint_dir=checkpoint_dir, progress_callback=progress_callback,
        prompt_builder=prompt_builder, retry_plan=retry_plan,
        _runtime=PDFOCRRuntime(
            page_count=get_pdf_page_count, render_page=render_pdf_page_as_png_data_url,
            load_checkpoints=load_codex_ocr_page_checkpoints, load_sources=load_codex_ocr_page_sources,
            write_source=write_codex_ocr_page_source, write_checkpoint=write_codex_ocr_page_checkpoint,
        ),
    )
    if result.status != "success":
        assert result.error is not None
        raise result.error
    return result.text, list(result.warnings)


def run_agy_pdf_ocr(reference_path: Path, loaded_settings: LoadedSettings) -> tuple[str, list[str]]:
    if shutil.which("agy") is None:
        raise AgyOCRError("未找到 agy CLI，无法执行 agy OCR。")

    reference_settings = loaded_settings.settings.reference
    models_to_try = [reference_settings.gemini_ocr_model]
    fallback_model = reference_settings.gemini_ocr_fallback_model.strip()
    if fallback_model and fallback_model not in models_to_try:
        models_to_try.append(fallback_model)

    workspace_dir, staged_pdf_path = build_agy_ocr_workspace(reference_path, loaded_settings)
    prompt = build_gemini_ocr_prompt(staged_pdf_path.name)
    last_error: str | None = None

    for index, model_name in enumerate(models_to_try):
        command = [
            "agy",
            "--model",
            model_name,
            "--print",
            prompt,
            "--print-timeout",
            f"{reference_settings.ocr_timeout_seconds}s",
        ]
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                cwd=str(workspace_dir),
                timeout=loaded_settings.settings.reference.ocr_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyOCRError(f"agy OCR 超时: {reference_path.name}") from exc
        except OSError as exc:
            raise AgyOCRError(f"agy OCR 启动失败: {reference_path.name} | {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            last_error = stderr or f"agy exited with code {completed.returncode}"
            if is_gemini_capacity_error(last_error) and index < len(models_to_try) - 1:
                continue
            raise AgyOCRError(f"agy OCR 失败: {reference_path.name} | {last_error}")

        text = sanitize_gemini_ocr_text(completed.stdout)
        if is_effectively_empty_text(text):
            last_error = "agy OCR 返回为空或接近空"
            continue

        ocr_dir = ensure_directory(loaded_settings.path_for("ocr_dir"))
        sidecar_path = ocr_dir / f"{reference_path.stem}.agy_ocr.txt"
        sidecar_path.write_text(text, encoding="utf-8")
        return text, [f"PDF 文字层为空，已使用 agy OCR fallback。model={model_name}"]

    raise AgyOCRError(f"agy OCR 未返回有效文本: {reference_path.name} | {last_error or 'unknown error'}")


def run_codex_pdf_ocr(reference_path: Path, loaded_settings: LoadedSettings) -> tuple[str, list[str]]:
    if shutil.which("codex") is None:
        raise CodexOCRError("未找到 codex CLI，无法执行 Codex OCR。")

    reference_settings = loaded_settings.settings.reference
    workspace_dir, staged_pdf_path = build_codex_ocr_workspace(reference_path, loaded_settings)
    prompt = build_codex_ocr_prompt(staged_pdf_path.name)

    command = [
        "codex",
        "exec",
        "-C",
        str(workspace_dir.resolve()),
        "-s",
        "read-only",
    ]
    configured_model = reference_settings.codex_ocr_model.strip()
    configured_reasoning_effort = reference_settings.codex_ocr_reasoning_effort.strip()
    if configured_model:
        command.extend(["-m", configured_model])
    if configured_reasoning_effort:
        command.extend(["-c", f'model_reasoning_effort="{configured_reasoning_effort}"'])

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt", delete=True, dir=workspace_dir) as output_file:
        command.extend(["-o", output_file.name, "-"])
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=str(workspace_dir),
                timeout=reference_settings.ocr_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexOCRError(f"Codex OCR 超时: {reference_path.name}") from exc
        except OSError as exc:
            raise CodexOCRError(f"Codex OCR 启动失败: {reference_path.name} | {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            raise CodexOCRError(f"Codex OCR 失败: {reference_path.name} | {stderr or f'codex exited with code {completed.returncode}'}")

        output_file.seek(0)
        text = sanitize_gemini_ocr_text(output_file.read())
        if is_effectively_empty_text(text):
            raise CodexOCRError(f"Codex OCR 未返回有效文本: {reference_path.name}")

    ocr_dir = ensure_directory(loaded_settings.path_for("ocr_dir"))
    sidecar_path = ocr_dir / f"{reference_path.stem}.codex_ocr.txt"
    sidecar_path.write_text(text, encoding="utf-8")
    return text, [f"PDF 文字层为空，已使用 Codex OCR fallback。model={configured_model or 'codex_default'}"]


def run_pdf_ai_ocr_backend(reference_path: Path, loaded_settings: LoadedSettings, backend: str) -> tuple[str, str, list[str]]:
    if backend == AI_OCR_BACKEND_CODEX_API:
        text, warnings = run_codex_api_pdf_ocr(reference_path, loaded_settings)
        return text, ai_ocr_method_name(backend), warnings
    if backend == AI_OCR_BACKEND_AGY:
        text, warnings = run_agy_pdf_ocr(reference_path, loaded_settings)
        return text, ai_ocr_method_name(backend), warnings
    if backend == AI_OCR_BACKEND_CODEX_CLI:
        text, warnings = run_codex_pdf_ocr(reference_path, loaded_settings)
        return text, ai_ocr_method_name(backend), warnings
    raise ReferencePreparationError(f"未知 PDF AI OCR 后端: {backend}")


def run_tesseract_pdf_ocr(reference_path: Path, loaded_settings: LoadedSettings) -> tuple[str, list[str]]:
    if shutil.which("ocrmypdf") is None:
        raise ReferencePreparationError("未找到 ocrmypdf，无法对扫描版 PDF 执行 OCR。")
    if shutil.which("tesseract") is None:
        raise ReferencePreparationError("未找到 tesseract，无法对扫描版 PDF 执行 OCR。")

    ocr_dir = ensure_directory(loaded_settings.path_for("ocr_dir"))
    ocr_pdf_path = ocr_dir / f"{reference_path.stem}.ocr.pdf"
    sidecar_path = ocr_dir / f"{reference_path.stem}.ocr.txt"

    if ocr_pdf_path.exists():
        ocr_pdf_path.unlink()
    if sidecar_path.exists():
        sidecar_path.unlink()

    command = [
        "ocrmypdf",
        "--skip-text",
        "--sidecar",
        str(sidecar_path),
        "-l",
        get_ocr_language_code(loaded_settings),
        str(reference_path),
        str(ocr_pdf_path),
    ]

    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ReferencePreparationError(f"OCR 命令执行失败: {reference_path.name} | {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ReferencePreparationError(f"OCR 处理失败: {reference_path.name} | {stderr}")

    if not sidecar_path.exists():
        raise ReferencePreparationError(f"OCR 未生成 sidecar 文本: {reference_path.name}")

    text = sanitize_ocrmypdf_text(read_text_file(sidecar_path))
    sidecar_path.write_text(text, encoding="utf-8")
    warnings = ["PDF 文字层为空，已使用 OCR fallback。backend=ocrmypdf_tesseract"]
    if is_effectively_empty_text(text):
        warnings.append("OCR 结果为空或接近空，当前 PDF 可能质量较差。")

    return text, warnings


def read_txt_reference(reference_path: Path) -> tuple[str, str, list[str]]:
    return read_text_file(reference_path), "direct_text_read", []


def read_md_reference(reference_path: Path) -> tuple[str, str, list[str]]:
    return read_text_file(reference_path), "direct_markdown_read", []


def read_pdf_reference(
    reference_path: Path,
    loaded_settings: LoadedSettings,
    *,
    sidecar_path: Path | None = None,
    checkpoint_dir: Path | None = None,
    progress_callback: Callable[[CodexOCRPageProgress], None] | None = None,
    gateway_connection=None,
) -> tuple[str, str, list[str]]:
    primary_backend = loaded_settings.settings.reference.ai_ocr_backend.strip() or AI_OCR_BACKEND_CODEX_API
    if primary_backend not in VALID_AI_OCR_BACKENDS:
        raise ReferencePreparationError(f"未知 PDF AI OCR 后端: {primary_backend}")
    if primary_backend == AI_OCR_BACKEND_CODEX_API:
        text, warnings = run_codex_api_pdf_ocr(
            reference_path,
            loaded_settings,
            sidecar_path=sidecar_path,
            checkpoint_dir=checkpoint_dir,
            progress_callback=progress_callback,
            **({"gateway_connection": gateway_connection} if gateway_connection is not None else {}),
        )
        return text, ai_ocr_method_name(primary_backend), warnings
    return run_pdf_ai_ocr_backend(reference_path, loaded_settings, primary_backend)


def prepare_reference_file(
    reference_path: Path,
    loaded_settings: LoadedSettings,
    logger: logging.Logger | None = None,
    *,
    checkpoint_root: Path | None = None,
    progress_callback: Callable[[ReferenceFileProgress], None] | None = None,
    gateway_connection=None,
) -> ReferenceFileResult:
    source_type = reference_path.suffix.lower().lstrip(".")
    output_paths = build_reference_output_paths(
        reference_path,
        loaded_settings.path_for("extracted_text_dir"),
    )
    handlers: dict[str, Callable[[Path], tuple[str, str, list[str]]]] = {
        "txt": read_txt_reference,
        "md": read_md_reference,
    }
    handler = handlers.get(source_type)
    if source_type == "pdf":
        primary_backend = loaded_settings.settings.reference.ai_ocr_backend.strip() or AI_OCR_BACKEND_CODEX_API
        extraction_method = ai_ocr_method_name(primary_backend)
        last_progress: CodexOCRPageProgress | None = None

        def pdf_progress(progress: CodexOCRPageProgress) -> None:
            nonlocal last_progress
            last_progress = progress
            if progress_callback:
                progress_callback(
                    ReferenceFileProgress(
                        source_path=reference_path,
                        output_text_path=output_paths.txt_path,
                        page_count=progress.page_count,
                        completed_pages=progress.completed_pages,
                        failed_page_numbers=progress.failed_page_numbers,
                        page_errors=progress.page_errors,
                        resumable=progress.resumable,
                        status=progress.status,
                    )
                )

        checkpoint_dir: Path | None = None
        if primary_backend == AI_OCR_BACKEND_CODEX_API:
            identity = build_pdf_ocr_run_identity(reference_path, loaded_settings)
            effective_checkpoint_root = checkpoint_root or (
                loaded_settings.path_for("ocr_dir") / "pdf-pages"
            )
            checkpoint_dir = build_pdf_ocr_checkpoint_namespace(
                effective_checkpoint_root,
                identity,
            )
        try:
            extracted_text, extraction_method, warnings = read_pdf_reference(
                reference_path,
                loaded_settings,
                sidecar_path=output_paths.txt_path,
                checkpoint_dir=checkpoint_dir,
                progress_callback=pdf_progress,
                **({"gateway_connection": gateway_connection} if gateway_connection is not None else {}),
            )
        except CodexOCRPagesIncompleteError as exc:
            result = ReferenceFileResult(
                source_file=build_source_file_label(reference_path, loaded_settings),
                source_type=source_type,
                output_text_file=output_paths.txt_path.name,
                extraction_method=extraction_method,
                success=False,
                text_length=0,
                warnings=[],
                extracted_text="",
                page_count=exc.page_count,
                completed_pages=len(exc.completed_page_numbers),
                failed_page_numbers=tuple(exc.page_errors),
                page_errors=exc.page_errors,
                resumable=True,
                error=str(exc),
            )
            if logger:
                logger.warning(
                    "参考 PDF OCR 部分完成 | %s | pages=%s/%s | failed=%s",
                    reference_path.name,
                    result.completed_pages,
                    result.page_count,
                    ",".join(str(page) for page in result.failed_page_numbers),
                )
            return result
        except ReferencePreparationError as exc:
            progress = last_progress or CodexOCRPageProgress(page_count=0)
            result = ReferenceFileResult(
                source_file=build_source_file_label(reference_path, loaded_settings),
                source_type=source_type,
                output_text_file=output_paths.txt_path.name,
                extraction_method=extraction_method,
                success=False,
                text_length=0,
                warnings=[],
                extracted_text="",
                page_count=progress.page_count,
                completed_pages=progress.completed_pages,
                failed_page_numbers=progress.failed_page_numbers,
                page_errors=progress.page_errors,
                resumable=progress.resumable and progress.page_count > 0,
                error=str(exc),
            )
            if logger:
                logger.error("参考 PDF OCR 失败 | %s | %s", reference_path.name, exc)
            return result
    elif handler is None:
        raise ReferencePreparationError(f"不支持的参考文件类型: {reference_path.name}")
    else:
        extracted_text, extraction_method, warnings = handler(reference_path)

    success = True

    if logger:
        logger.info("参考文件处理完成 | %s | success=%s", reference_path.name, success)
    completed_page_count = last_progress.completed_pages if source_type == "pdf" and last_progress else 0
    page_count = last_progress.page_count if source_type == "pdf" and last_progress else 0

    return ReferenceFileResult(
        source_file=build_source_file_label(reference_path, loaded_settings),
        source_type=source_type,
        output_text_file=output_paths.txt_path.name,
        extraction_method=extraction_method,
        success=success,
        text_length=len(extracted_text),
        warnings=warnings,
        extracted_text=extracted_text,
        page_count=page_count,
        completed_pages=completed_page_count,
    )


def write_reference_result(result: ReferenceFileResult, output_paths: ReferenceOutputPaths) -> None:
    ensure_directory(output_paths.txt_path.parent)

    if result.success:
        with output_paths.txt_path.open("w", encoding="utf-8") as file:
            file.write(result.extracted_text)
    elif output_paths.txt_path.exists():
        output_paths.txt_path.unlink()

    payload = {
        "source_file": result.source_file,
        "source_type": result.source_type,
        "output_text_file": result.output_text_file,
        "extraction_method": result.extraction_method,
        "success": result.success,
        "text_length": result.text_length,
        "warnings": result.warnings,
        "error": result.error or "",
        "page_count": result.page_count,
        "completed_pages": result.completed_pages,
        "failed_page_numbers": list(result.failed_page_numbers),
        "page_errors": {str(page_number): error for page_number, error in result.page_errors.items()},
        "resumable": result.resumable,
    }
    with output_paths.json_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def prepare_reference_batch(
    loaded_settings: LoadedSettings,
    logger: logging.Logger | None = None,
    *,
    checkpoint_root: Path | None = None,
    progress_callback: Callable[[ReferenceFileProgress], None] | None = None,
    gateway_connection=None,
) -> ReferenceBatchSummary:
    reference_dir = loaded_settings.path_for("reference_dir")
    output_dir = ensure_directory(loaded_settings.path_for("extracted_text_dir"))
    allowed_extensions = get_supported_reference_extensions(loaded_settings)
    reference_files = iter_reference_files(reference_dir, allowed_extensions)

    if not reference_files:
        supported_ext = ", ".join(allowed_extensions)
        raise ReferenceInputEmptyError(
            f"输入目录中没有可处理的参考文件: {reference_dir}。支持扩展名: {supported_ext}"
        )

    items: list[ReferenceBatchItem] = []
    success_count = 0
    skipped_count = 0
    failed_count = 0

    for reference_path in reference_files:
        output_paths = build_reference_output_paths(reference_path, output_dir)
        result = prepare_reference_file(
            reference_path,
            loaded_settings,
            logger=logger,
            checkpoint_root=checkpoint_root,
            progress_callback=progress_callback,
            **({"gateway_connection": gateway_connection} if gateway_connection is not None else {}),
        )
        write_reference_result(result, output_paths)

        if result.success:
            success_count += 1
        elif result.resumable:
            skipped_count += 1
        else:
            failed_count += 1

        items.append(
            ReferenceBatchItem(
                source_path=reference_path,
                output_paths=output_paths,
                success=result.success,
                warnings=result.warnings,
                error=result.error,
                page_count=result.page_count,
                completed_pages=result.completed_pages,
                failed_page_numbers=result.failed_page_numbers,
                page_errors=result.page_errors,
                resumable=result.resumable,
            )
        )

    return ReferenceBatchSummary(
        total=len(reference_files),
        success=success_count,
        skipped=skipped_count,
        failed=failed_count,
        items=items,
    )


def summarize_reference_results(summary: ReferenceBatchSummary) -> str:
    return (
        f"total={summary.total}, success={summary.success}, "
        f"partial={summary.partial}, skipped={summary.skipped}, failed={summary.failed}"
    )

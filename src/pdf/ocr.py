"""单 PDF 逐页 OCR：显式模型与网关、可恢复检查点及完整产物发布。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Protocol

from src.llm.gateway import CodexLBClient
from src.ocr_page_markers import format_ocr_page_break
from src.ocr_scheduler import OCRPageTask, run_staggered_page_ocr_tasks
from src.pdf.errors import CodexOCRError, CodexOCRPagesIncompleteError, PdfDependencyError, ReferencePreparationError
from src.pdf.workflow import PDFOCRPageModel, PDFOCRPageState, PDFOCRRetryPlan, build_pdf_ocr_run_identity, select_pdf_ocr_page_model
from src.runtime_utils import ensure_directory

LOGGER = logging.getLogger(__name__)

META_LINE_MARKERS = (
    "CRITICAL INSTRUCTION",
    "EPHEMERAL_MESSAGE",
    "Now I have the text content",
    "Let's read the text content",
    "OK, I will output",
    "I will read through carefully",
    "I just need to return it",
    "Page 1 ends with",
    "The following is an ephemeral message",
    "你现在要对一份中文 PDF 做 OCR 提取",
    "任务要求",
    "只输出提取后的纯文本",
    "不要解释",
    "请处理这个 PDF 文件",
    "我将读取 PDF 文件",
)


def import_pdf_reader() -> Callable[[str], object]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfDependencyError("未安装 pypdf。请先执行 `pip install -r requirements.txt`。") from exc

    return PdfReader


def strip_fenced_text(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate)
    return candidate.strip()


def is_page_marker_line(text: str) -> bool:
    stripped = text.strip()
    return bool(re.fullmatch(r"\[Page\s+\d+\]", stripped, flags=re.IGNORECASE)) or bool(
        re.fullmatch(r"Page\s+\d+:?", stripped, flags=re.IGNORECASE)
    )


def is_page_number_line(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}", text.strip()))


def is_meta_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    normalized = stripped.lower()
    return any(marker.lower() in normalized for marker in META_LINE_MARKERS)


def sanitize_gemini_ocr_text(text: str) -> str:
    candidate = strip_fenced_text(text)
    output_lines: list[str] = []
    started = False

    for raw_line in candidate.splitlines():
        line = raw_line.strip()
        if not line:
            if started and output_lines and output_lines[-1] != "":
                output_lines.append("")
            continue

        if is_meta_line(line):
            if started:
                break
            continue

        if is_page_marker_line(line):
            started = True
            continue

        if is_page_number_line(line):
            if started:
                continue
            continue

        if not started and re.fullmatch(r"[A-Za-z]{1,2}>\.{2,}", line):
            continue

        started = True
        output_lines.append(re.sub(r"^t(?=[\u3400-\u9fff])", "", line))

    while output_lines and output_lines[-1] == "":
        output_lines.pop()

    return "\n".join(output_lines).strip()


def build_codex_api_image_ocr_prompt(reference_path: str | Path, page_number: int, page_count: int) -> str:
    return "\n".join(
        [
            "你现在要对一份中文 PDF 的单页图片做 OCR 提取。",
            "任务要求：",
            "1. 只输出提取后的纯文本。",
            "2. 不要解释，不要总结，不要添加说明。",
            "3. 保留原文顺序，尽量保留自然段。",
            "4. 不要输出 Markdown，不要输出 JSON。",
            "5. 不要输出页码、分页标记、页眉、页尾、Page 1 之类的分页提示。",
            "6. 本次只提供当前这一页图片，请直接识别其中的正文。",
            "",
            f"PDF 文件名：{Path(reference_path).name}",
            f"当前页：{page_number}",
            f"PDF 总页数：{page_count}",
        ]
    )


def build_pdf_book_ocr_image_prompt(reference_path: str | Path, page_number: int, page_count: int) -> str:
    """转写中外文混排页面；保留调度接口参数，但不附加文件名和页数语境。"""
    return "\n".join(
        [
            "这是一个 OCR（图像文字识别）任务。请识别我提供的这张图片中实际可见的文字，并输出纯文本。",
            "页面可能有中文、英文及其他语言混排：各语言保持原文，不翻译、不音译，不因切换语言而省略文字。",
            "按自然阅读顺序保留标题、段落和换行；同一行或段落内的中外文按原位置衔接，不按语言重新分组。",
            "外文保留原有大小写、重音符号、标点和引号，不凭记忆补全熟悉的引文。",
            "保留正文、脚注、尾注、边注、编者注、译者注及其编号或符号；小字和页面底部的外文注释也要识别，并与正文分行。",
            "仅省略明确的页码、统一页眉和页尾；无法判断是否为注释时保留。",
            "只依据图片，不补写图片外的内容，不猜测或改写看不清的文字，不总结、不解释，不输出 Markdown 或 JSON。",
        ]
    )


def get_pdf_page_count(reference_path: Path) -> int:
    PdfReader = import_pdf_reader()
    try:
        page_count = len(PdfReader(str(reference_path)).pages)
    except Exception as exc:
        raise CodexOCRError(f"Codex API OCR 无法读取 PDF 页数: {reference_path.name} | {exc}") from exc
    if page_count < 1:
        raise CodexOCRError(f"Codex API OCR PDF 没有可处理的页面: {reference_path.name}")
    return page_count


def render_pdf_page_as_png_data_url(reference_path: Path, page_number: int, page_count: int) -> str:
    if shutil.which("pdftoppm") is None:
        raise CodexOCRError("未找到 pdftoppm，无法将 PDF 页面渲染为图片供 Codex API OCR。")
    if page_number < 1 or page_number > page_count:
        raise CodexOCRError(
            f"Codex API OCR 页面超出范围: {reference_path.name} | 页码 {page_number}，总页数 {page_count}"
        )

    with tempfile.TemporaryDirectory(prefix=f"{reference_path.stem}.codex_api_page_{page_number}_") as temp_dir:
        output_prefix = Path(temp_dir) / "page"
        command = [
            "pdftoppm",
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-singlefile",
            "-png",
            str(reference_path),
            str(output_prefix),
        ]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            raise CodexOCRError(
                f"Codex API OCR 第 {page_number} 页渲染失败: {reference_path.name} | "
                f"{stderr or f'pdftoppm exited with code {completed.returncode}'}"
            )

        page_path = output_prefix.with_suffix(".png")
        if not page_path.is_file():
            raise CodexOCRError(f"Codex API OCR 页面渲染未生成第 {page_number} 页图片: {reference_path.name}")
        try:
            encoded = base64.b64encode(page_path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise CodexOCRError(f"Codex API OCR 无法读取第 {page_number} 页渲染图片: {exc}") from exc
        return f"data:image/png;base64,{encoded}"


def codex_ocr_page_checkpoint_path(checkpoint_dir: Path, page_number: int) -> Path:
    return checkpoint_dir / f"page-{page_number:06d}.txt"


def load_codex_ocr_page_checkpoints(checkpoint_dir: Path, page_count: int) -> dict[int, str]:
    page_texts: dict[int, str] = {}
    for page_number in range(1, page_count + 1):
        page_path = codex_ocr_page_checkpoint_path(checkpoint_dir, page_number)
        if not page_path.is_file():
            continue
        try:
            page_texts[page_number] = page_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CodexOCRError(f"无法读取 OCR 第 {page_number} 页检查点: {page_path} | {exc}") from exc
    return page_texts


def write_codex_ocr_page_checkpoint(checkpoint_dir: Path, page_number: int, page_text: str) -> None:
    ensure_directory(checkpoint_dir)
    page_path = codex_ocr_page_checkpoint_path(checkpoint_dir, page_number)
    pending_path = page_path.with_suffix(".txt.pending")
    try:
        pending_path.write_text(page_text, encoding="utf-8")
        pending_path.replace(page_path)
    except OSError as exc:
        raise CodexOCRError(f"无法写入 OCR 第 {page_number} 页检查点: {page_path} | {exc}") from exc


def load_codex_ocr_page_sources(
    checkpoint_dir: Path, page_texts: dict[int, str], primary_model: str,
) -> dict[int, str]:
    """旧 TXT 没有来源 sidecar 仍可复用；已有来源必须与文本及父模型一致。"""
    fallback_models: dict[int, str] = {}
    for page, text in page_texts.items():
        source_path = checkpoint_dir / f"page-{page:06d}.source.json"
        if not source_path.exists():
            continue
        try:
            source = json.loads(source_path.read_text(encoding="utf-8"))
            if (not isinstance(source, dict)
                    or not isinstance(source.get("model"), str)
                    or not source["model"].strip()
                    or source.get("primary_model") != primary_model
                    or source.get("text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
                raise ValueError("来源与页文本或父模型不一致")
        except (OSError, ValueError) as exc:
            raise CodexOCRError(f"无法校验 OCR 第 {page} 页模型来源: {source_path} | {exc}") from exc
        if source.get("fallback") is True:
            fallback_models[page] = source["model"]
    return fallback_models


def write_codex_ocr_page_source(
    checkpoint_dir: Path, page: int, text: str,
    primary: PDFOCRPageModel, effective: PDFOCRPageModel,
) -> None:
    source_path = checkpoint_dir / f"page-{page:06d}.source.json"
    pending_path = source_path.with_suffix(".json.pending")
    source = {
        "schema_version": 1,
        "page_number": page,
        "primary_model": primary.model,
        "model": effective.model,
        "reasoning_effort": effective.reasoning_effort,
        "fallback": effective.model != primary.model,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    try:
        ensure_directory(checkpoint_dir)
        pending_path.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
        pending_path.replace(source_path)
    except OSError as exc:
        raise CodexOCRError(f"无法写入 OCR 第 {page} 页模型来源: {source_path} | {exc}") from exc


class OCRGateway(Protocol):
    def responses_stream_text(self, payload: dict[str, object]) -> str: ...


@dataclass(frozen=True)
class PDFOCROptions:
    model: str
    reasoning_effort: str = ""
    fast_mode: bool = False
    max_concurrency: int = 1
    submit_interval_seconds: float = 0.0


@dataclass(frozen=True)
class PDFOCRResult:
    status: Literal["success", "partial", "failed"]
    source_pdf: Path
    output_text_path: Path
    state: PDFOCRPageState
    text: str = ""
    warnings: tuple[str, ...] = ()
    error: ReferencePreparationError | None = None

    @property
    def success(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class PDFOCRRuntime:
    """显式文件操作接缝，供历史适配器与可控测试使用。"""
    page_count: Callable = get_pdf_page_count
    render_page: Callable = render_pdf_page_as_png_data_url
    load_checkpoints: Callable = load_codex_ocr_page_checkpoints
    load_sources: Callable = load_codex_ocr_page_sources
    write_source: Callable = write_codex_ocr_page_source
    write_checkpoint: Callable = write_codex_ocr_page_checkpoint


def merge_pdf_pages(page_texts: list[str]) -> str:
    """按物理页合并，空白页仍保留边界。"""
    parts: list[str] = []
    for page_number, page_text in enumerate(page_texts, start=1):
        if page_number > 1:
            parts.append(format_ocr_page_break(page_number - 1))
        normalized = page_text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if normalized.strip():
            parts.append(normalized)
    return "\n\n".join(parts)


def ocr_pdf(
    source_pdf: Path,
    output_text_path: Path,
    options: PDFOCROptions,
    *,
    gateway: OCRGateway,
    checkpoint_dir: Path | None = None,
    progress_callback: Callable[[PDFOCRPageState], None] | None = None,
    prompt_builder: Callable[[str | Path, int, int], str] = build_codex_api_image_ocr_prompt,
    retry_plan: PDFOCRRetryPlan | None = None,
    _runtime: PDFOCRRuntime | None = None,
) -> PDFOCRResult:
    """识别单 PDF；只有全部页面完成且 TXT 原子发布后才返回 success。

    checkpoint_dir 是调用方选择的身份目录；可用 workflow 中的身份/命名空间
    helper 创建。重试计划只消费显式传入且与本 PDF/主模型身份匹配的计划。
    """
    runtime = _runtime or PDFOCRRuntime()
    if isinstance(gateway, CodexLBClient):
        # OCR 必须由 completed 流提交成功页，即使调用者传了通用客户端的默认设置。
        gateway = replace(gateway, require_stream_completed=True)
    source_pdf, output_text_path = Path(source_pdf), Path(output_text_path)
    primary = PDFOCRPageModel(options.model.strip(), options.reasoning_effort.strip(), options.fast_mode)
    completed: dict[int, str] = {}
    fallback_models: dict[int, str] = {}
    errors: dict[int, str] = {}
    page_count = 0

    def state(status: str = "running") -> PDFOCRPageState:
        return PDFOCRPageState(page_count, tuple(sorted(completed)), dict(sorted(errors.items())),
                               dict(sorted(fallback_models.items())), status=status)

    def report(status: str = "running") -> None:
        if progress_callback:
            progress_callback(state(status))

    try:
        if not primary.model:
            raise CodexOCRError("PDF OCR 需要明确指定 model。")
        if options.max_concurrency < 1 or options.submit_interval_seconds < 0:
            raise CodexOCRError("PDF OCR 并发数必须为正整数，投递间隔不能小于 0。")
        if retry_plan is not None and retry_plan.identity_fingerprint != build_pdf_ocr_run_identity(
            source_pdf, primary.model, primary.reasoning_effort,
        ).fingerprint:
            retry_plan = None
        page_count = runtime.page_count(source_pdf)
        if checkpoint_dir is not None:
            completed.update(runtime.load_checkpoints(checkpoint_dir, page_count))
            fallback_models.update(runtime.load_sources(checkpoint_dir, completed, primary.model))
        tasks = [OCRPageTask(page) for page in range(1, page_count + 1) if page not in completed]
        report()

        def recognize(task: OCRPageTask) -> str:
            effective = select_pdf_ocr_page_model(primary, task.page_number, retry_plan)
            payload: dict[str, object] = {
                "model": effective.model,
                "input": [{"role": "user", "content": [
                    {"type": "input_text", "text": prompt_builder(source_pdf.name, task.page_number, page_count)},
                    {"type": "input_image", "image_url": runtime.render_page(source_pdf, task.page_number, page_count)},
                ]}],
                "stream": True, "store": False,
            }
            if effective.reasoning_effort:
                payload["reasoning"] = {"effort": effective.reasoning_effort}
            if effective.fast_mode:
                payload["service_tier"] = "priority"
            return sanitize_gemini_ocr_text(strip_fenced_text(gateway.responses_stream_text(payload)))

        def succeeded(task: OCRPageTask, text: str) -> None:
            effective = select_pdf_ocr_page_model(primary, task.page_number, retry_plan)
            if checkpoint_dir is not None:
                runtime.write_source(checkpoint_dir, task.page_number, text, primary, effective)
                runtime.write_checkpoint(checkpoint_dir, task.page_number, text)
            if effective.model != primary.model:
                fallback_models[task.page_number] = effective.model
            completed[task.page_number] = text
            errors.pop(task.page_number, None)
            report()

        def failed(task: OCRPageTask, error: Exception) -> None:
            errors[task.page_number] = str(error)
            report()

        page_run = run_staggered_page_ocr_tasks(
            tasks, recognize, max_concurrency=options.max_concurrency,
            submit_interval_seconds=options.submit_interval_seconds,
            logger=LOGGER, on_succeeded=succeeded, on_failed=failed,
        )
        for page, error in page_run.errors_by_page.items():
            errors.setdefault(page, str(error))
        missing = [page for page in range(1, page_count + 1) if page not in completed]
        if missing:
            for page in missing:
                errors.setdefault(page, "本轮未生成成功页结果。")
            raise CodexOCRPagesIncompleteError(source_pdf, page_count, list(completed), errors,
                                               fallback_page_models=fallback_models)
        text = merge_pdf_pages([completed[page] for page in range(1, page_count + 1)])
        ensure_directory(output_text_path.parent)
        pending = output_text_path.with_suffix(output_text_path.suffix + ".pending")
        pending.write_text(text, encoding="utf-8")
        pending.replace(output_text_path)
        warnings = [f"已使用 Codex API OCR。model={primary.model}；已完成 {len(completed)}/{page_count} 页。"]
        if fallback_models:
            pages = "、".join(str(page) for page in sorted(fallback_models))
            warnings.append(f"上游输出失败页已使用 Gemini 补页；页码={pages}；来源见逐页 sidecar。")
        report("success")
        return PDFOCRResult("success", source_pdf, output_text_path, state("success"), text, tuple(warnings))
    except (ReferencePreparationError, OSError) as exc:
        error = exc if isinstance(exc, ReferencePreparationError) else CodexOCRError(str(exc))
        status = "partial" if isinstance(exc, CodexOCRPagesIncompleteError) else "failed"
        report(status)
        return PDFOCRResult(status, source_pdf, output_text_path, state(status), error=error)

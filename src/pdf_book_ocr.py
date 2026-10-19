from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from src.reference_utils import (
    CodexOCRError,
    CodexOCRPageProgress,
    CodexOCRPagesIncompleteError,
    build_pdf_book_ocr_image_prompt,
    run_codex_api_pdf_ocr,
)
from src.pdf_ocr_workflow import (
    PDFOCRRetryPlan,
    build_pdf_ocr_checkpoint_namespace,
    build_pdf_ocr_run_identity,
)
from src.schemas import LoadedSettings


class PDFBookOCRError(RuntimeError):
    """PDF 书籍 OCR 的输入或执行准备失败。"""


@dataclass(frozen=True)
class PDFBookOCRItem:
    source_pdf: Path
    output_text_path: Path
    success: bool
    text_length: int
    warnings: list[str]
    error: str | None = None
    page_count: int = 0
    completed_pages: int = 0
    failed_page_numbers: tuple[int, ...] = ()
    page_errors: dict[int, str] = field(default_factory=dict)
    fallback_page_models: dict[int, str] = field(default_factory=dict)
    ocr_identity: str = ""


@dataclass(frozen=True)
class PDFBookOCRProgress:
    source_pdf: Path
    output_text_path: Path
    page_count: int
    completed_pages: int
    failed_page_numbers: tuple[int, ...]
    page_errors: dict[int, str]
    fallback_page_models: dict[int, str] = field(default_factory=dict)
    ocr_identity: str = ""


@dataclass(frozen=True)
class PDFBookOCRSummary:
    items: list[PDFBookOCRItem]

    @property
    def success_count(self) -> int:
        return sum(item.success for item in self.items)

    @property
    def failure_count(self) -> int:
        return len(self.items) - self.success_count


def iter_pdf_book_files(input_path: Path) -> list[Path]:
    """返回单个 PDF 或目录中的全部 PDF，并保持稳定的相对路径顺序。"""
    resolved_input_path = input_path.expanduser().resolve()
    if resolved_input_path.is_file():
        if resolved_input_path.suffix.lower() != ".pdf":
            raise PDFBookOCRError(f"输入文件不是 PDF: {resolved_input_path}")
        return [resolved_input_path]

    if not resolved_input_path.is_dir():
        raise PDFBookOCRError(f"输入路径不存在或不是文件夹: {resolved_input_path}")

    pdf_files = sorted(
        (
            path.resolve()
            for path in resolved_input_path.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: path.relative_to(resolved_input_path).as_posix().lower(),
    )
    if not pdf_files:
        raise PDFBookOCRError(f"输入文件夹中没有找到 PDF: {resolved_input_path}")
    return pdf_files


def pdf_book_ocr_source_label(source_pdf: Path, input_path: Path) -> str:
    if input_path.is_dir():
        return source_pdf.resolve().relative_to(input_path.resolve()).as_posix()
    return source_pdf.name


def build_pdf_book_output_path(input_path: Path, source_pdf: Path, output_dir: Path) -> Path:
    """为单本或批量书籍构造 TXT 输出路径，批量模式保留目录层级。"""
    resolved_input_path = input_path.expanduser().resolve()
    resolved_source_pdf = source_pdf.expanduser().resolve()
    resolved_output_dir = output_dir.expanduser().resolve()

    if resolved_input_path.is_dir():
        try:
            relative_pdf_path = resolved_source_pdf.relative_to(resolved_input_path)
        except ValueError as exc:
            raise PDFBookOCRError(
                f"PDF 不在输入文件夹内，无法确定输出路径: {resolved_source_pdf}"
            ) from exc
    else:
        relative_pdf_path = Path(resolved_source_pdf.name)

    return resolved_output_dir / relative_pdf_path.with_suffix(".txt")


def build_pdf_book_checkpoint_dir(input_path: Path, source_pdf: Path, checkpoint_root: Path) -> Path:
    resolved_input_path = input_path.expanduser().resolve()
    resolved_source_pdf = source_pdf.expanduser().resolve()
    resolved_checkpoint_root = checkpoint_root.expanduser().resolve()
    if resolved_input_path.is_dir():
        relative_pdf_path = resolved_source_pdf.relative_to(resolved_input_path)
    else:
        relative_pdf_path = Path(resolved_source_pdf.name)
    return resolved_checkpoint_root / relative_pdf_path.parent / f"{relative_pdf_path.stem}.pages"


def ocr_pdf_book(
    source_pdf: Path,
    output_text_path: Path,
    loaded_settings: LoadedSettings,
    *,
    checkpoint_dir: Path | None = None,
    progress_callback: Callable[[PDFBookOCRProgress], None] | None = None,
    retry_plan: PDFOCRRetryPlan | None = None,
) -> PDFBookOCRItem:
    """识别单本 PDF；只有整本各页都成功时才写出该书 TXT。"""
    resolved_source_pdf = source_pdf.expanduser().resolve()
    resolved_output_text_path = output_text_path.expanduser().resolve()
    last_progress: CodexOCRPageProgress | None = None

    def page_progress(progress: CodexOCRPageProgress) -> None:
        nonlocal last_progress
        last_progress = progress
        if progress_callback:
            progress_callback(
                PDFBookOCRProgress(
                    source_pdf=resolved_source_pdf,
                    output_text_path=resolved_output_text_path,
                    page_count=progress.page_count,
                    completed_pages=len(progress.completed_page_numbers),
                    failed_page_numbers=tuple(progress.page_errors),
                    page_errors=progress.page_errors,
                    fallback_page_models=progress.fallback_page_models,
                )
            )

    try:
        text, warnings = run_codex_api_pdf_ocr(
            resolved_source_pdf,
            loaded_settings,
            sidecar_path=resolved_output_text_path,
            checkpoint_dir=checkpoint_dir,
            progress_callback=page_progress,
            prompt_builder=build_pdf_book_ocr_image_prompt,
            **({"retry_plan": retry_plan} if retry_plan is not None else {}),
        )
    except CodexOCRPagesIncompleteError as exc:
        return PDFBookOCRItem(
            source_pdf=resolved_source_pdf,
            output_text_path=resolved_output_text_path,
            success=False,
            text_length=0,
            warnings=[],
            error=str(exc),
            page_count=exc.page_count,
            completed_pages=len(exc.completed_page_numbers),
            failed_page_numbers=tuple(exc.page_errors),
            page_errors=exc.page_errors,
            fallback_page_models=exc.fallback_page_models,
        )
    except CodexOCRError as exc:
        return PDFBookOCRItem(
            source_pdf=resolved_source_pdf,
            output_text_path=resolved_output_text_path,
            success=False,
            text_length=0,
            warnings=[],
            error=str(exc),
            page_count=last_progress.page_count if last_progress else 0,
            completed_pages=len(last_progress.completed_page_numbers) if last_progress else 0,
            failed_page_numbers=tuple(last_progress.page_errors) if last_progress else (),
            page_errors=last_progress.page_errors if last_progress else {},
        )

    return PDFBookOCRItem(
        source_pdf=resolved_source_pdf,
        output_text_path=resolved_output_text_path,
        success=True,
        text_length=len(text),
        warnings=warnings,
        page_count=last_progress.page_count if last_progress else 0,
        completed_pages=len(last_progress.completed_page_numbers) if last_progress else 0,
        fallback_page_models=last_progress.fallback_page_models if last_progress else {},
    )


def ocr_pdf_book_batch(
    input_path: Path,
    output_dir: Path,
    loaded_settings: LoadedSettings,
    *,
    checkpoint_root: Path | None = None,
    progress_callback: Callable[[PDFBookOCRProgress], None] | None = None,
    retry_plans: dict[str, PDFOCRRetryPlan] | None = None,
) -> PDFBookOCRSummary:
    """批量识别 PDF 书籍；一本失败不会阻止其余书籍继续处理。"""
    source_pdfs = iter_pdf_book_files(input_path)
    items: list[PDFBookOCRItem] = []
    for source_pdf in source_pdfs:
        identity = (
            build_pdf_ocr_run_identity(source_pdf, loaded_settings)
            if checkpoint_root is not None or retry_plans else None
        )
        checkpoint_dir = (
            build_pdf_ocr_checkpoint_namespace(
                build_pdf_book_checkpoint_dir(input_path, source_pdf, checkpoint_root), identity,
            )
            if checkpoint_root is not None else None
        )
        plan = (retry_plans or {}).get(pdf_book_ocr_source_label(source_pdf, input_path))
        if plan is not None and (identity is None or plan.identity_fingerprint != identity.fingerprint):
            plan = None

        def report_book_progress(progress: PDFBookOCRProgress) -> None:
            if progress_callback:
                progress_callback(replace(progress, ocr_identity=identity.fingerprint) if identity else progress)

        item = ocr_pdf_book(
            source_pdf,
            build_pdf_book_output_path(input_path, source_pdf, output_dir),
            loaded_settings,
            checkpoint_dir=checkpoint_dir,
            progress_callback=report_book_progress if progress_callback else None,
            **({"retry_plan": plan} if plan is not None else {}),
        )
        items.append(replace(item, ocr_identity=identity.fingerprint) if identity else item)
    return PDFBookOCRSummary(items=items)


def summarize_pdf_book_ocr(summary: PDFBookOCRSummary) -> str:
    return f"total={len(summary.items)}, success={summary.success_count}, failed={summary.failure_count}"

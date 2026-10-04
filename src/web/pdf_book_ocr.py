from __future__ import annotations

import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from src.web.uploads import upload_root

PDF_BOOK_OCR_TASK_ID_PATTERN = re.compile(r"pdf-ocr-[0-9a-f]{32}")


class PDFBookOCRTaskError(ValueError):
    """PDF 书籍 OCR Web 任务的路径或标识无效。"""


@dataclass(frozen=True)
class PDFBookOCRTaskPaths:
    task_id: str
    task_root: Path
    state_path: Path
    output_dir: Path
    checkpoint_dir: Path


def create_pdf_book_ocr_task_id() -> str:
    return f"pdf-ocr-{uuid4().hex}"


def build_pdf_book_ocr_task_paths(project_root: Path, task_id: str) -> PDFBookOCRTaskPaths:
    if not PDF_BOOK_OCR_TASK_ID_PATTERN.fullmatch(task_id):
        raise PDFBookOCRTaskError(f"无效的 PDF OCR 任务 ID: {task_id}")

    tasks_root = (project_root / "data/jobs/pdf-ocr").resolve()
    task_root = tasks_root / task_id
    if task_root.is_symlink() or task_root.resolve().parent != tasks_root:
        raise PDFBookOCRTaskError("PDF OCR 任务目录无效。")
    return PDFBookOCRTaskPaths(
        task_id=task_id,
        task_root=task_root,
        state_path=task_root / "state.json",
        output_dir=task_root / "output",
        checkpoint_dir=task_root / "pages",
    )


def pdf_book_ocr_retry_payload(state: dict) -> dict[str, object]:
    request_payload = state.get("request_payload")
    if isinstance(request_payload, dict) and isinstance(request_payload.get("input_path"), str):
        return dict(request_payload)

    input_summary = state.get("input_summary")
    input_path = input_summary.get("input_path") if isinstance(input_summary, dict) else None
    if not isinstance(input_path, str) or not input_path.strip():
        raise PDFBookOCRTaskError("PDF OCR 历史任务缺少输入路径，无法重试。")
    return {"input_path": input_path}


def resolve_uploaded_pdf_ocr_input(project_root: Path, input_path: str) -> Path:
    source_path = Path(input_path).expanduser().resolve()
    allowed_root = (upload_root(project_root) / "pdf-ocr").resolve()

    try:
        source_path.relative_to(allowed_root)
    except ValueError as exc:
        raise PDFBookOCRTaskError("PDF OCR 仅支持使用本页面上传的 PDF 文件或目录。") from exc

    if source_path.is_file():
        if source_path.suffix.lower() != ".pdf":
            raise PDFBookOCRTaskError(f"PDF OCR 输入文件不是 PDF: {source_path.name}")
        return source_path
    if source_path.is_dir():
        return source_path
    raise PDFBookOCRTaskError(f"PDF OCR 输入路径不存在: {source_path}")


def relative_pdf_book_ocr_output_path(task_paths: PDFBookOCRTaskPaths, output_path: Path) -> str:
    try:
        return output_path.resolve().relative_to(task_paths.output_dir.resolve()).as_posix()
    except ValueError as exc:
        raise PDFBookOCRTaskError("PDF OCR 输出路径不在当前任务目录内。") from exc


def resolve_pdf_book_ocr_output_file(task_paths: PDFBookOCRTaskPaths, relative_path: str) -> Path:
    requested_path = Path(relative_path)
    if not relative_path or requested_path.is_absolute():
        raise PDFBookOCRTaskError("PDF OCR 下载路径无效。")

    output_root = task_paths.output_dir.resolve()
    if not output_root.is_relative_to(task_paths.task_root.resolve()):
        raise PDFBookOCRTaskError("PDF OCR 输出目录超出当前任务目录。")
    candidate = (output_root / requested_path).resolve()
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise PDFBookOCRTaskError("PDF OCR 下载路径超出当前任务输出目录。") from exc

    if candidate.suffix.lower() != ".txt":
        raise PDFBookOCRTaskError("PDF OCR 仅支持下载 TXT 结果。")
    return candidate


def build_pdf_book_ocr_archive(task_paths: PDFBookOCRTaskPaths, state: dict) -> bytes:
    """只打包完整书籍，保留相对目录，附带未完成书籍清单。"""
    files: dict[str, Path] = {}
    incomplete: list[str] = []
    for item in state.get("items") or []:
        if not isinstance(item, dict):
            continue
        if not item.get("success"):
            incomplete.append(str(item.get("source_file") or "未知书籍"))
            continue
        output_path = resolve_pdf_book_ocr_output_file(task_paths, str(item.get("output_file") or ""))
        if not output_path.is_file():
            raise PDFBookOCRTaskError(f"完整书籍的 TXT 文件缺失：{item.get('output_file')}")
        relative_name = output_path.relative_to(task_paths.output_dir.resolve()).as_posix()
        files[relative_name] = output_path
    if not files:
        raise PDFBookOCRTaskError("当前任务暂无完整书籍可下载，请先完成识别或重试缺失页。")

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for relative_name, output_path in files.items():
            archive.write(output_path, f"books/{relative_name}")
        archive.writestr(
            "summary.json",
            json.dumps(
                {
                    "task_id": task_paths.task_id,
                    "status": state.get("status"),
                    "included_books": list(files),
                    "incomplete_books": incomplete,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return buffer.getvalue()

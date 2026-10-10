from __future__ import annotations

from pathlib import Path

import pytest

from src.pdf_book_ocr import PDFBookOCRItem, PDFBookOCRSummary
from src.web.pdf_book_ocr import (
    PDFBookOCRTaskError,
    build_pdf_book_ocr_task_paths,
    create_pdf_book_ocr_task_id,
    resolve_pdf_book_ocr_output_file,
    resolve_uploaded_pdf_ocr_input,
    pdf_book_ocr_retry_payload,
)
from src.web.state_store import create_initial_state, read_json_file, write_json_file
from tests.helpers import write_minimal_settings


def test_resolve_uploaded_pdf_ocr_input_rejects_path_outside_pdf_ocr_upload_root(tmp_path: Path) -> None:
    source = tmp_path / "outside.pdf"
    source.write_bytes(b"pdf")

    with pytest.raises(PDFBookOCRTaskError, match="本页面上传"):
        resolve_uploaded_pdf_ocr_input(tmp_path, str(source))


def test_resolve_pdf_book_ocr_output_file_rejects_path_traversal(tmp_path: Path) -> None:
    task_paths = build_pdf_book_ocr_task_paths(tmp_path, create_pdf_book_ocr_task_id())

    with pytest.raises(PDFBookOCRTaskError, match="超出"):
        resolve_pdf_book_ocr_output_file(task_paths, "../state.txt")


def test_pdf_book_ocr_retry_payload_supports_legacy_task_state() -> None:
    payload = pdf_book_ocr_retry_payload({"input_summary": {"input_path": "/uploads/book.pdf"}})

    assert payload == {"input_path": "/uploads/book.pdf", "ocr_fast_mode": False}
    for enabled in (False, True):
        request = {"input_path": "/uploads/book.pdf", "ocr_fast_mode": enabled}
        assert pdf_book_ocr_retry_payload({"request_payload": request}) == request


def test_execute_pdf_book_ocr_writes_isolated_task_state_and_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api_server import create_app
    from src.web.tasks import execute_pdf_book_ocr

    write_minimal_settings(tmp_path)
    input_path = tmp_path / "data/uploads/pdf-ocr/20260713/group-001/哲学史.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"%PDF-1.4 fake")

    app = create_app(project_root=tmp_path)
    task_id = create_pdf_book_ocr_task_id()
    task_paths = build_pdf_book_ocr_task_paths(tmp_path, task_id)
    write_json_file(task_paths.state_path, create_initial_state(task_id, "pdf-ocr"))

    def fake_ocr_pdf_book_batch(
        source: Path,
        output_dir: Path,
        loaded_settings,
        *,
        checkpoint_root: Path,
        progress_callback,
    ) -> PDFBookOCRSummary:
        assert source == input_path.resolve()
        assert checkpoint_root == task_paths.checkpoint_dir
        assert loaded_settings.settings.reference.codex_ocr_max_concurrency == 12
        assert loaded_settings.settings.reference.codex_ocr_submit_interval_seconds == 2.5
        _ = progress_callback
        output_path = output_dir / "哲学史.txt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("OCR 识别结果", encoding="utf-8")
        return PDFBookOCRSummary(
            items=[
                PDFBookOCRItem(
                    source_pdf=source,
                    output_text_path=output_path,
                    success=True,
                    text_length=len("OCR 识别结果"),
                    warnings=["已完成"],
                    page_count=2,
                    completed_pages=2,
                )
            ]
        )

    monkeypatch.setattr("src.web.tasks.ocr_pdf_book_batch", fake_ocr_pdf_book_batch)

    execute_pdf_book_ocr(
        app=app,
        task_id=task_id,
        payload={
            "input_path": str(input_path),
            "ocr_max_concurrency": 12,
            "ocr_submit_interval_seconds": 2.5,
        },
    )

    state = read_json_file(task_paths.state_path)
    assert state["status"] == "success"
    assert state["current_stage"] == "done"
    assert state["total"] == 1
    assert state["success"] == 1
    assert state["failed"] == 0
    assert state["items"] == [
        {
            "source_file": "哲学史.pdf",
            "output_file": "哲学史.txt",
            "success": True,
            "text_length": len("OCR 识别结果"),
            "warnings": ["已完成"],
            "error": "",
            "page_count": 2,
            "completed_pages": 2,
            "failed_pages": 0,
            "failed_page_numbers": [],
            "page_errors": {},
            "resumable": False,
        }
    ]
    assert (task_paths.output_dir / "哲学史.txt").read_text(encoding="utf-8") == "OCR 识别结果"


def test_execute_pdf_book_ocr_marks_missing_pages_as_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from api_server import create_app
    from src.web.tasks import execute_pdf_book_ocr

    write_minimal_settings(tmp_path)
    input_dir = tmp_path / "data/uploads/pdf-ocr/20260713/group-001"
    input_dir.mkdir(parents=True, exist_ok=True)
    first_pdf = input_dir / "第一册.pdf"
    second_pdf = input_dir / "第二册.pdf"
    first_pdf.write_bytes(b"%PDF-1.4 first")
    second_pdf.write_bytes(b"%PDF-1.4 second")

    app = create_app(project_root=tmp_path)
    task_id = create_pdf_book_ocr_task_id()
    task_paths = build_pdf_book_ocr_task_paths(tmp_path, task_id)
    write_json_file(task_paths.state_path, create_initial_state(task_id, "pdf-ocr"))

    def fake_ocr_pdf_book_batch(
        source: Path,
        output_dir: Path,
        _loaded_settings,
        *,
        checkpoint_root: Path,
        progress_callback,
    ) -> PDFBookOCRSummary:
        _ = source, checkpoint_root, progress_callback
        output_path = output_dir / "第一册.txt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("第一册文本", encoding="utf-8")
        return PDFBookOCRSummary(
            items=[
                PDFBookOCRItem(
                    source_pdf=first_pdf,
                    output_text_path=output_path,
                    success=True,
                    text_length=len("第一册文本"),
                    warnings=[],
                    page_count=2,
                    completed_pages=2,
                ),
                PDFBookOCRItem(
                    source_pdf=second_pdf,
                    output_text_path=output_dir / "第二册.txt",
                    success=False,
                    text_length=0,
                    warnings=[],
                    error="第二册第 3 页识别失败",
                    page_count=4,
                    completed_pages=3,
                    failed_page_numbers=(3,),
                    page_errors={3: "upstream_unavailable"},
                ),
            ]
        )

    monkeypatch.setattr("src.web.tasks.ocr_pdf_book_batch", fake_ocr_pdf_book_batch)

    execute_pdf_book_ocr(app=app, task_id=task_id, payload={"input_path": str(input_dir)})

    state = read_json_file(task_paths.state_path)
    assert state["status"] == "partial"
    assert state["success"] == 1
    assert state["failed"] == 1
    assert state["pages_total"] == 6
    assert state["pages_completed"] == 5
    assert state["pages_failed"] == 1
    assert "仍有 1 页待重试" in state["error_message"]
    assert state["items"][0]["output_file"] == "第一册.txt"
    assert state["items"][1]["error"] == "第二册第 3 页识别失败"
    assert state["items"][1]["failed_page_numbers"] == [3]
    assert state["items"][1]["page_errors"] == {"3": "upstream_unavailable"}

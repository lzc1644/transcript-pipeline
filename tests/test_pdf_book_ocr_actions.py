from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import threading
from zipfile import ZipFile

import httpx
import pytest

from api_server import create_app
from src.config_loader import load_settings
from src.reference_utils import run_codex_api_pdf_ocr, write_codex_ocr_page_checkpoint
from src.web.pdf_book_ocr import build_pdf_book_ocr_task_paths, create_pdf_book_ocr_task_id
from src.web.state_store import create_initial_state, write_json_file
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json


def make_task(root: Path, *, status: str = "success", items: list | None = None):
    paths = build_pdf_book_ocr_task_paths(root, create_pdf_book_ocr_task_id())
    state = create_initial_state(paths.task_id, "pdf-ocr")
    state.update(status=status, items=items or [])
    write_json_file(paths.state_path, state)
    return paths


def test_delete_pdf_ocr_removes_only_task_and_preserves_uploads_and_other_tasks(tmp_path):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path)
    other = make_task(tmp_path)
    upload = tmp_path / "data/uploads/pdf-ocr/group/book.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"original PDF")
    paths.checkpoint_dir.mkdir()
    (paths.checkpoint_dir / "page-000001.txt").write_text("正文")
    paths.output_dir.mkdir()
    (paths.output_dir / "book.txt").write_text("正文")

    response = request_json(app, "DELETE", f"/api/pdf-book-ocr/{paths.task_id}")
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert not paths.task_root.exists()
    assert other.state_path.exists()
    assert upload.read_bytes() == b"original PDF"
    assert request_json(app, "GET", f"/api/pdf-book-ocr/{paths.task_id}").status_code == 404
    assert request_json(app, "GET", "/api/pdf-book-ocr").json()["items"][0]["id"] == other.task_id
    assert request_json(app, "DELETE", f"/api/pdf-book-ocr/{paths.task_id}").status_code == 404


@pytest.mark.parametrize("status", ["pending", "running"])
@pytest.mark.parametrize("method, suffix", [("DELETE", ""), ("GET", "/download")])
def test_pdf_ocr_active_task_cannot_be_deleted_or_archived(tmp_path, status, method, suffix):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path, status=status)
    app.state.active_jobs.add(paths.task_id)
    response = request_json(app, method, f"/api/pdf-book-ocr/{paths.task_id}{suffix}")
    assert response.status_code == 409
    assert paths.state_path.exists()


def test_pdf_ocr_interrupted_task_can_be_deleted(tmp_path):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path, status="running")
    assert request_json(app, "DELETE", f"/api/pdf-book-ocr/{paths.task_id}").status_code == 200


@pytest.mark.parametrize("method, suffix", [("DELETE", ""), ("GET", "/download")])
def test_pdf_ocr_actions_reject_unknown_or_symlink_task(tmp_path, method, suffix):
    app = create_app(project_root=tmp_path)
    task_id = create_pdf_book_ocr_task_id()
    assert request_json(app, method, f"/api/pdf-book-ocr/{task_id}{suffix}").status_code == 404
    assert request_json(app, method, f"/api/pdf-book-ocr/invalid{suffix}").status_code == 404
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}")
    paths = build_pdf_book_ocr_task_paths(tmp_path, task_id)
    paths.task_root.parent.mkdir(parents=True, exist_ok=True)
    paths.task_root.symlink_to(outside, target_is_directory=True)
    assert request_json(app, method, f"/api/pdf-book-ocr/{task_id}{suffix}").status_code == 404
    assert (outside / "state.json").exists()


def test_pdf_ocr_archive_preserves_nested_duplicate_book_names_and_reports_incomplete(tmp_path):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path, status="partial", items=[
        {"source_file": "上册/书.pdf", "output_file": "上册/书.txt", "success": True},
        {"source_file": "下册/书.pdf", "output_file": "下册/书.txt", "success": True},
        {"source_file": "缺页.pdf", "output_file": "缺页.txt", "success": False},
    ])
    for relative in ["上册/书.txt", "下册/书.txt", "缺页.txt", "未登记.txt"]:
        output = paths.output_dir / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("标题\n\n自然段一。\n自然段二。", encoding="utf-8")
    response = request_json(app, "GET", f"/api/pdf-book-ocr/{paths.task_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "-txt.zip" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"books/上册/书.txt", "books/下册/书.txt", "summary.json"}
        assert archive.read("books/上册/书.txt").decode("utf-8") == "标题\n\n自然段一。\n自然段二。"
        summary = json.loads(archive.read("summary.json"))
        assert summary["incomplete_books"] == ["缺页.pdf"]
        assert summary["status"] == "partial"


@pytest.mark.parametrize("relative", ["../outside.txt", "/outside.txt", "book.epub", "missing.txt", ""])
def test_pdf_ocr_archive_rejects_unsafe_or_missing_successful_outputs(tmp_path, relative):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path, items=[{"success": True, "output_file": relative}])
    response = request_json(app, "GET", f"/api/pdf-book-ocr/{paths.task_id}/download")
    assert response.status_code == 409


@pytest.mark.parametrize("symlink_dir", [False, True])
def test_pdf_ocr_archive_rejects_symlink_outside_task(tmp_path, symlink_dir):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path, items=[{"success": True, "output_file": "book.txt"}])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "book.txt").write_text("private text")
    if symlink_dir:
        paths.output_dir.symlink_to(outside, target_is_directory=True)
    else:
        paths.output_dir.mkdir()
        (paths.output_dir / "book.txt").symlink_to(outside / "book.txt")
    assert request_json(app, "GET", f"/api/pdf-book-ocr/{paths.task_id}/download").status_code == 409


def test_pdf_ocr_archive_without_complete_books_is_explicit_error(tmp_path):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path, status="failed", items=[{"success": False, "source_file": "缺页.pdf"}])
    response = request_json(app, "GET", f"/api/pdf-book-ocr/{paths.task_id}/download")
    assert response.status_code == 409
    assert "暂无完整书籍" in response.text


def test_pdf_ocr_archive_compression_does_not_block_event_loop(tmp_path, monkeypatch):
    app = create_app(project_root=tmp_path)
    paths = make_task(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_archive(*_args):
        started.set()
        assert release.wait(timeout=5)
        return b"zip bytes"

    monkeypatch.setattr("api_server.build_pdf_book_ocr_archive", slow_archive)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            download = asyncio.create_task(client.get(f"/api/pdf-book-ocr/{paths.task_id}/download"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                health = await asyncio.wait_for(client.get("/api/health"), timeout=1)
                assert health.status_code == 200
                assert not download.done()
            finally:
                release.set()
                result = await download
            assert result.status_code == 200

    asyncio.run(exercise())


def test_pdf_ocr_cached_pages_preserve_paragraphs_notes_and_normalize_line_endings(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    settings = load_settings(project_root=tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"PDF")
    checkpoints = tmp_path / "pages"
    pages = ["标题\r\n\r\n第一段。\r\n第二段。\r\n① 注释。", "", "下一页第一段。\n\n下一段。"]
    for number, text in enumerate(pages, 1):
        write_codex_ocr_page_checkpoint(checkpoints, number, text)
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _: len(pages))

    def no_remote(*_args):
        pytest.fail("缓存全部存在，不应调用模型或渲染 PDF")

    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", no_remote)
    monkeypatch.setattr("src.reference_utils.CodexLBClient.responses_stream_text", no_remote)
    output = tmp_path / "output/book.txt"
    text, _ = run_codex_api_pdf_ocr(source, settings, sidecar_path=output, checkpoint_dir=checkpoints)
    assert text == (
        "标题\n\n第一段。\n第二段。\n① 注释。\n\n"
        "----- OCR_PAGE_BREAK: 1 -> 2 -----\n\n"
        "----- OCR_PAGE_BREAK: 2 -> 3 -----\n\n"
        "下一页第一段。\n\n下一段。"
    )
    assert output.read_text(encoding="utf-8") == text
    assert source.read_bytes() == b"PDF"
    assert (checkpoints / "page-000002.txt").read_text() == ""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from api_server import create_app
from src.codex_lb_client import CodexLBClientError, extract_event_stream_text
from src.config_loader import load_settings
from src.pdf_book_ocr import build_pdf_book_checkpoint_dir, ocr_pdf_book_batch
from src.pdf_ocr_workflow import (
    PDFOCRPageModel,
    PDFOCRRetryPlan,
    PDFOCRRunIdentity,
    PDF_OCR_RETRY_FALLBACK_MODEL,
    build_pdf_ocr_checkpoint_namespace,
    build_pdf_ocr_retry_plan,
    build_pdf_ocr_run_identity,
    is_upstream_ocr_output_error,
    select_pdf_ocr_page_model,
)
from src.reference_utils import CodexOCRError, write_codex_ocr_page_checkpoint
from src.web.frontend_settings import FrontendSettingsUpdate, save_frontend_settings
from src.web.pdf_book_ocr import build_pdf_book_ocr_task_paths, create_pdf_book_ocr_task_id
from src.web.state_store import create_initial_state, read_json_file, write_json_file
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json


@pytest.mark.parametrize("error", [
    "response.incomplete: {'reason': 'content_filter'}",
    "response.incomplete: max_output_tokens",
    'HTTP 400: {"code":"safety_violation"}',
    "regurg_plugin blocked the output stream.",
    "stream disconnected before completion: regurg plugin blocked the output stream.",
    "Responses API 响应流未完整结束：缺少 response.completed",
])
def test_output_failures_qualify_for_retry_fallback(error):
    assert is_upstream_ocr_output_error(error)


@pytest.mark.parametrize("error", [
    "curl: (35) TLS connect error: unexpected EOF",
    "curl: (28) Operation timed out",
    "HTTP 429 rate limit",
    "HTTP 500 upstream_unavailable",
    "HTTP 401 invalid_api_key",
    "缺少 API 网关 API Key 环境变量",
    "Responses API 响应未完整传输: IncompleteRead(100 bytes read)",
    "stream disconnected before completion: connection reset by peer",
    "connection refused",
])
def test_transport_auth_and_capacity_failures_keep_primary_model(error):
    assert not is_upstream_ocr_output_error(error)


def test_retry_plan_and_page_selection_are_identity_bound_and_do_not_change_primary():
    identity = PDFOCRRunIdentity("digest", "gpt-6-luna", "xhigh")
    errors = {2: "content_filter", 3: "TLS connect error"}
    old = PDFOCRRetryPlan(identity.fingerprint, (4,))
    plan = build_pdf_ocr_retry_plan(identity, errors, old)
    assert plan.page_numbers == (2, 4)
    assert PDFOCRRetryPlan.from_dict(plan.to_dict()) == plan
    primary = PDFOCRPageModel("gpt-6-luna", "xhigh", True)
    assert select_pdf_ocr_page_model(primary, 3, plan) is primary
    assert select_pdf_ocr_page_model(primary, 2) is primary
    assert select_pdf_ocr_page_model(primary, 2, plan) == PDFOCRPageModel(
        PDF_OCR_RETRY_FALLBACK_MODEL, "medium", False,
    )
    changed = PDFOCRRunIdentity("different PDF", "gpt-6-luna", "xhigh")
    assert build_pdf_ocr_retry_plan(changed, {}, old).page_numbers == ()
    gemini = PDFOCRRunIdentity("digest", PDF_OCR_RETRY_FALLBACK_MODEL, "medium")
    assert build_pdf_ocr_retry_plan(gemini, errors).page_numbers == ()


def event_stream(text: str, *, completed: bool = True) -> str:
    events = [{"type": "response.output_text.delta", "delta": text}]
    if completed:
        events.append({"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": text},
            ]}],
        }})
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)


@pytest.mark.parametrize("error_type", ["error", "response.error", "response.incomplete"])
def test_explicit_error_events_never_accept_partial_text(error_type):
    raw = event_stream("只有半页。", completed=False)
    raw += "data: " + json.dumps({"type": error_type, "error": {"code": "safety_violation"}}) + "\n\n"
    with pytest.raises(CodexLBClientError, match="失败事件"):
        extract_event_stream_text(raw)


def test_strict_ocr_requires_completed_without_changing_legacy_client_default():
    raw = event_stream("只有半页。", completed=False)
    assert extract_event_stream_text(raw) == "只有半页。"
    with pytest.raises(CodexLBClientError, match="响应流未完整结束"):
        extract_event_stream_text(raw, require_completed=True)
    assert extract_event_stream_text(event_stream(""), require_completed=True) == ""


@pytest.fixture
def setup_ocr(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path, reference_overrides={
        "codex_ocr_model": "gpt-6-luna",
        "codex_ocr_reasoning_effort": "xhigh",
        "codex_ocr_fast_mode": True,
        "codex_ocr_max_concurrency": 1,
        "codex_ocr_submit_interval_seconds": 0,
    })
    loaded = load_settings(project_root=tmp_path)
    source = tmp_path / "data/uploads/pdf-ocr/group/book.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf fixture")
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_LB_BASE_URL", "http://127.0.0.1:2455")
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda path: 3)

    def render(path, page, count):
        return "data:image/png;base64," + base64.b64encode(f"{path.parent.name}:{page}".encode()).decode()

    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", render)
    return tmp_path, loaded, source


def request_parts(request):
    payload = json.loads(request.data)
    encoded = payload["input"][0]["content"][1]["image_url"].split(",", 1)[1]
    label, page = base64.b64decode(encoded).decode().rsplit(":", 1)
    return payload, label, int(page)


def checkpoint_dir(source, loaded, root):
    return build_pdf_ocr_checkpoint_namespace(
        build_pdf_book_checkpoint_dir(source, source, root), build_pdf_ocr_run_identity(source, loaded),
    )


def test_retry_only_switches_selected_missing_pages_and_preserves_legacy_checkpoints(setup_ocr, monkeypatch):
    tmp, loaded, source = setup_ocr
    root = tmp / "pages"
    pages = checkpoint_dir(source, loaded, root)
    write_codex_ocr_page_checkpoint(pages, 1, "已有 GPT 正文。")
    cached = pages / "page-000001.txt"
    snapshot = cached.read_bytes(), cached.stat().st_mtime_ns
    identity = build_pdf_ocr_run_identity(source, loaded)
    plan = build_pdf_ocr_retry_plan(identity, {2: "content_filter", 3: "TLS connect error"})
    seen = []

    def read(request, **kwargs):
        payload, _, page = request_parts(request)
        seen.append((page, payload))
        return event_stream(f"第{page}页正文。\n（30）English footnote.")

    monkeypatch.setattr("src.llm.gateway.read_http_response", read)
    summary = ocr_pdf_book_batch(source, tmp / "output", loaded, checkpoint_root=root,
                                retry_plans={source.name: plan})
    assert summary.success_count == 1
    assert [page for page, _ in seen] == [2, 3]
    assert seen[0][1]["model"] == PDF_OCR_RETRY_FALLBACK_MODEL
    assert seen[0][1]["reasoning"] == {"effort": "medium"}
    assert "service_tier" not in seen[0][1]
    assert seen[1][1]["model"] == "gpt-6-luna"
    assert seen[1][1]["reasoning"] == {"effort": "xhigh"}
    assert seen[1][1]["service_tier"] == "priority"
    assert (cached.read_bytes(), cached.stat().st_mtime_ns) == snapshot
    assert summary.items[0].fallback_page_models == {2: PDF_OCR_RETRY_FALLBACK_MODEL}
    provenance = json.loads((pages / "page-000002.source.json").read_text())
    assert provenance["model"] == PDF_OCR_RETRY_FALLBACK_MODEL
    assert provenance["primary_model"] == "gpt-6-luna" and provenance["fallback"] is True
    assert loaded.settings.reference.codex_ocr_model == "gpt-6-luna"
    assert loaded.settings.reference.codex_ocr_reasoning_effort == "xhigh"
    assert list(pages.parent.iterdir()) == [pages]
    seen.clear()
    again = ocr_pdf_book_batch(source, tmp / "output", loaded, checkpoint_root=root)
    assert not seen
    assert again.items[0].fallback_page_models == {2: PDF_OCR_RETRY_FALLBACK_MODEL}
    assert (cached.read_bytes(), cached.stat().st_mtime_ns) == snapshot
    # 已有来源的文本被篡改时，不能静默丢失 provenance 后继续认定成功。
    (pages / "page-000002.txt").write_text("被替换的文本。")
    damaged = ocr_pdf_book_batch(source, tmp / "output", loaded, checkpoint_root=root)
    assert damaged.failure_count == 1 and "模型来源" in damaged.items[0].error
    assert not seen


@pytest.mark.parametrize("failure", ["content_filter", "missing_completed", "source_write_failed"])
def test_failed_fallback_never_commits_success_page_or_book(setup_ocr, monkeypatch, failure):
    tmp, loaded, source = setup_ocr
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda path: 1)
    plan = build_pdf_ocr_retry_plan(build_pdf_ocr_run_identity(source, loaded), {1: "content_filter"})
    calls = []

    def read(request, **kwargs):
        payload, _, _ = request_parts(request)
        calls.append(payload["model"])
        if failure == "content_filter":
            return 'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"content_filter"}}}\n\n'
        return event_stream("候选正文。", completed=failure != "missing_completed")

    monkeypatch.setattr("src.llm.gateway.read_http_response", read)
    if failure == "source_write_failed":
        def fail_source(*args):
            raise CodexOCRError("无法写入模型来源")
        monkeypatch.setattr("src.reference_utils.write_codex_ocr_page_source", fail_source)
    summary = ocr_pdf_book_batch(source, tmp / "output", loaded, checkpoint_root=tmp / "pages",
                                retry_plans={source.name: plan})
    assert summary.failure_count == 1 and summary.items[0].completed_pages == 0
    assert calls == [PDF_OCR_RETRY_FALLBACK_MODEL]
    assert not list((tmp / "pages").rglob("page-*.txt"))
    assert not (tmp / "output/book.txt").exists()


def test_plans_are_isolated_by_source_and_reject_changed_identity(setup_ocr, monkeypatch):
    tmp, loaded, _ = setup_ocr
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda path: 1)
    inputs = tmp / "books"
    first = inputs / "a/book.pdf"
    second = inputs / "b/book.pdf"
    for index, path in enumerate((first, second)):
        path.parent.mkdir(parents=True)
        path.write_bytes(f"different pdf {index}".encode())
    plan = build_pdf_ocr_retry_plan(build_pdf_ocr_run_identity(first, loaded), {1: "content_filter"})
    seen = []

    def read(request, **kwargs):
        payload, label, _ = request_parts(request)
        seen.append((label, payload["model"]))
        return event_stream("完整正文。")

    monkeypatch.setattr("src.llm.gateway.read_http_response", read)
    ocr_pdf_book_batch(inputs, tmp / "output", loaded, checkpoint_root=tmp / "pages",
                      retry_plans={"a/book.pdf": plan, "b/book.pdf": plan})
    assert seen == [("a", PDF_OCR_RETRY_FALLBACK_MODEL), ("b", "gpt-6-luna")]


def test_web_first_run_never_adds_fallback_and_click_retry_only_fills_missing_page(setup_ocr, monkeypatch):
    tmp, loaded, source = setup_ocr
    app = create_app(project_root=tmp, run_tasks_inline=True)
    seen = []

    def read(request, **kwargs):
        payload, _, page = request_parts(request)
        seen.append((page, payload["model"]))
        if page == 2 and payload["model"].startswith("gpt"):
            return 'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"content_filter"}}}\n\n'
        return event_stream(f"第{page}页正文及注释。")

    monkeypatch.setattr("src.llm.gateway.read_http_response", read)
    response = request_json(app, "POST", "/api/pdf-book-ocr", json_body={"input_path": str(source)})
    task_id = response.json()["task_id"]
    paths = build_pdf_book_ocr_task_paths(tmp, task_id)
    state = read_json_file(paths.state_path)
    assert state["status"] == "partial" and state["pages_completed"] == 2
    assert seen == [(1, "gpt-6-luna"), (2, "gpt-6-luna"), (3, "gpt-6-luna")]
    response = request_json(app, "POST", f"/api/pdf-book-ocr/{task_id}/retry")
    assert response.status_code == 202
    state = read_json_file(paths.state_path)
    assert state["status"] == "success" and state["pages_completed"] == 3
    assert seen[-1] == (2, PDF_OCR_RETRY_FALLBACK_MODEL) and len(seen) == 4
    assert state["request_payload"]["ocr_model"] == "gpt-6-luna"
    assert state["items"][0]["fallback_page_models"] == {"2": PDF_OCR_RETRY_FALLBACK_MODEL}
    assert request_json(app, "POST", f"/api/pdf-book-ocr/{task_id}/retry").status_code == 409


def test_legacy_failure_plan_survives_failed_gemini_retry_and_service_restart(setup_ocr, monkeypatch):
    tmp, loaded, source = setup_ocr
    app = create_app(project_root=tmp, run_tasks_inline=True)
    task_id = create_pdf_book_ocr_task_id()
    paths = build_pdf_book_ocr_task_paths(tmp, task_id)
    state = create_initial_state(task_id, "pdf-ocr")
    state.update({
        "status": "partial", "current_stage": "done",
        "request_payload": {"input_path": str(source), "ocr_model": "gpt-6-luna",
                            "ocr_reasoning_effort": "xhigh", "ocr_fast_mode": True,
                            "ocr_max_concurrency": 1, "ocr_submit_interval_seconds": 0},
        "items": [{"source_file": source.name, "page_errors": {"2": "content_filter", "3": "TLS error"}}],
    })
    write_json_file(paths.state_path, state)
    pages = checkpoint_dir(source, loaded, paths.checkpoint_dir)
    write_codex_ocr_page_checkpoint(pages, 1, "已有成功页。")
    snapshot = (pages / "page-000001.txt").stat().st_mtime_ns
    seen = []
    fail_gemini = True

    def read(request, **kwargs):
        payload, _, page = request_parts(request)
        seen.append((page, payload["model"]))
        if payload["model"] == PDF_OCR_RETRY_FALLBACK_MODEL and fail_gemini:
            raise CodexLBClientError("TLS connect error")
        return event_stream(f"第{page}页正文及注释。")

    monkeypatch.setattr("src.llm.gateway.read_http_response", read)
    assert request_json(app, "POST", f"/api/pdf-book-ocr/{task_id}/retry").status_code == 202
    partial = read_json_file(paths.state_path)
    assert partial["status"] == "partial"
    assert partial["ocr_retry_plans"][source.name]["page_numbers"] == [2]
    assert partial["items"][0]["page_errors"]["2"] == "TLS connect error"
    fail_gemini = False
    save_frontend_settings(tmp, FrontendSettingsUpdate(ocr_model="different-model", ocr_reasoning_effort="low"))
    restarted = create_app(project_root=tmp, run_tasks_inline=True)
    assert request_json(restarted, "POST", f"/api/pdf-book-ocr/{task_id}/retry").status_code == 202
    finished = read_json_file(paths.state_path)
    assert finished["status"] == "success"
    assert seen == [(2, PDF_OCR_RETRY_FALLBACK_MODEL), (3, "gpt-6-luna"), (2, PDF_OCR_RETRY_FALLBACK_MODEL)]
    assert finished["request_payload"]["ocr_model"] == "gpt-6-luna"
    assert finished["items"][0]["fallback_page_models"] == {"2": PDF_OCR_RETRY_FALLBACK_MODEL}
    assert (pages / "page-000001.txt").stat().st_mtime_ns == snapshot

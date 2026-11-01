"""书库使用的指定物理页 OCR：全部网关响应均为离线夹具。"""
import json
from pathlib import Path
from threading import Event

import pytest
from pypdf import PdfWriter

from src.ocr_scheduler import OCRPageTask, run_staggered_page_ocr_tasks
from src.pdf import PDFOCROptions, build_pdf_ocr_run_identity, ocr_pdf, ocr_pdf_pages
from src.pdf.errors import CodexOCRError
from src.pdf.ocr import (
    PDFOCRRuntime,
    write_codex_ocr_page_checkpoint,
    write_codex_ocr_page_source,
)
from src.pdf.workflow import PDFOCRPageModel, PDFOCRRetryPlan


@pytest.fixture
def source_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "original.pdf"
    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=100, height=100)
    with path.open("wb") as output:
        writer.write(output)
    return path


def runtime(rendered: list[tuple[Path, int, int]] | None = None) -> PDFOCRRuntime:
    def render(path: Path, page: int, page_count: int) -> str:
        if rendered is not None:
            rendered.append((path, page, page_count))
        return f"data:image/png;base64,page-{page}"

    return PDFOCRRuntime(render_page=render)


class FakeGateway:
    def __init__(self, texts: dict[int, str | Exception], stop_event: Event | None = None):
        self.texts = texts
        self.calls: list[dict] = []
        self.stop_event = stop_event

    def responses_stream_text(self, payload: dict) -> str:
        self.calls.append(payload)
        page = int(payload["input"][0]["content"][1]["image_url"].rsplit("-", 1)[1])
        if self.stop_event is not None:
            self.stop_event.set()
        response = self.texts[page]
        if isinstance(response, Exception):
            raise response
        return response


def pages_called(gateway: FakeGateway) -> list[int]:
    return [int(call["input"][0]["content"][1]["image_url"].rsplit("-", 1)[1])
            for call in gateway.calls]


def test_requested_physical_pages_are_deduplicated_and_callback_follows_checkpoint(source_pdf, tmp_path):
    checkpoint_dir = tmp_path / "pages"
    rendered = []
    gateway = FakeGateway({1: "first", 5: "last"})
    callbacks = []

    def on_page(page, text, source):
        assert (checkpoint_dir / f"page-{page:06d}.txt").read_text(encoding="utf-8") == text
        assert (checkpoint_dir / f"page-{page:06d}.source.json").is_file()
        callbacks.append((page, text, source))

    result = ocr_pdf_pages(source_pdf, [5, 1, 5], PDFOCROptions("gpt-fixture", "high"),
                           gateway=gateway, checkpoint_dir=checkpoint_dir,
                           page_callback=on_page, _runtime=runtime(rendered))

    assert result.page_count == 5
    assert result.texts_by_page == {1: "first", 5: "last"}
    assert result.errors_by_page == {} and not result.interrupted
    assert pages_called(gateway) == [1, 5]
    assert rendered == [(source_pdf, 1, 5), (source_pdf, 5, 5)]
    assert [item[0] for item in callbacks] == [1, 5]
    assert result.sources_by_page[5] == callbacks[1][2]
    assert result.sources_by_page[5]["model"] == "gpt-fixture"


def test_cached_blank_and_known_source_replayed_but_unselected_pages_never_read(source_pdf, tmp_path):
    checkpoints = tmp_path / "pages"
    write_codex_ocr_page_checkpoint(checkpoints, 1, "")
    primary = PDFOCRPageModel("gpt-fixture", "high")
    write_codex_ocr_page_source(checkpoints, 3, "cached", primary, primary)
    write_codex_ocr_page_checkpoint(checkpoints, 3, "cached")
    (checkpoints / "page-000002.txt").write_bytes(b"\xff")
    (checkpoints / "page-000002.source.json").write_text("not-json", encoding="utf-8")
    gateway = FakeGateway({5: "new"})
    delivered = []
    result = ocr_pdf_pages(source_pdf, [5, 3, 1], PDFOCROptions("gpt-fixture", "high"),
                           gateway=gateway, checkpoint_dir=checkpoints, _runtime=runtime(),
                           page_callback=lambda *args: delivered.append(args))

    assert result.texts_by_page == {1: "", 3: "cached", 5: "new"}
    assert pages_called(gateway) == [5]
    assert [item[0] for item in delivered] == [1, 3, 5]
    assert result.sources_by_page[1] == {"page_number": 1, "source_incomplete": True}
    assert result.sources_by_page[3]["model"] == "gpt-fixture"

    # 再次选择已准备页时仍通知调用方，且发出零个 OCR 请求。
    gateway = FakeGateway({})
    replayed = []
    complete = ocr_pdf_pages(source_pdf, [1, 3, 5], PDFOCROptions("gpt-fixture", "high"),
                             gateway=gateway, checkpoint_dir=checkpoints, _runtime=runtime(),
                             page_callback=lambda *args: replayed.append(args))
    assert pages_called(gateway) == []
    assert complete.texts_by_page == result.texts_by_page
    assert [item[0] for item in replayed] == [1, 3, 5]


@pytest.mark.parametrize("page", [0, -1, 6, "1", True])
def test_invalid_pages_rejected_before_any_request(source_pdf, page):
    gateway = FakeGateway({})
    with pytest.raises(CodexOCRError, match="页码"):
        ocr_pdf_pages(source_pdf, [1, page], PDFOCROptions("gpt-fixture"),
                      gateway=gateway, _runtime=runtime())
    assert not gateway.calls


def test_empty_selection_is_not_whole_book_and_none_is_rejected(source_pdf):
    gateway = FakeGateway({})
    result = ocr_pdf_pages(source_pdf, [], PDFOCROptions("gpt-fixture"),
                           gateway=gateway, _runtime=runtime())
    assert result.page_count == 5 and result.texts_by_page == {} and not gateway.calls
    with pytest.raises(CodexOCRError, match="明确的页码集合"):
        ocr_pdf_pages(source_pdf, None, PDFOCROptions("gpt-fixture"),
                      gateway=gateway, _runtime=runtime())


def test_selected_sidecar_mismatch_is_not_silently_reused(source_pdf, tmp_path):
    checkpoints = tmp_path / "pages"
    primary = PDFOCRPageModel("gpt-fixture", "high")
    write_codex_ocr_page_source(checkpoints, 3, "original", primary, primary)
    write_codex_ocr_page_checkpoint(checkpoints, 3, "changed")
    gateway = FakeGateway({})
    with pytest.raises(CodexOCRError, match="模型来源"):
        ocr_pdf_pages(source_pdf, [3], PDFOCROptions("gpt-fixture", "high"),
                      gateway=gateway, checkpoint_dir=checkpoints, _runtime=runtime())
    assert not gateway.calls


def test_selected_sidecar_with_wrong_page_number_is_rejected_without_request(source_pdf, tmp_path):
    checkpoints = tmp_path / "pages"
    primary = PDFOCRPageModel("gpt-fixture", "high")
    write_codex_ocr_page_source(checkpoints, 3, "cached", primary, primary)
    write_codex_ocr_page_checkpoint(checkpoints, 3, "cached")
    sidecar = checkpoints / "page-000003.source.json"
    source = json.loads(sidecar.read_text(encoding="utf-8"))
    source["page_number"] = 4
    sidecar.write_text(json.dumps(source), encoding="utf-8")
    gateway = FakeGateway({})
    with pytest.raises(CodexOCRError, match="来源页码不一致"):
        ocr_pdf_pages(source_pdf, [3], PDFOCROptions("gpt-fixture", "high"),
                      gateway=gateway, checkpoint_dir=checkpoints, _runtime=runtime())
    assert not gateway.calls


def test_whole_book_keeps_old_sidecar_contract_when_page_number_differs(source_pdf, tmp_path):
    checkpoints = tmp_path / "pages"
    primary = PDFOCRPageModel("gpt-fixture", "high")
    for page in range(1, 6):
        write_codex_ocr_page_checkpoint(checkpoints, page, f"page {page}")
    write_codex_ocr_page_source(checkpoints, 3, "page 3", primary, primary)
    sidecar = checkpoints / "page-000003.source.json"
    source = json.loads(sidecar.read_text(encoding="utf-8"))
    source["page_number"] = 4
    sidecar.write_text(json.dumps(source), encoding="utf-8")
    gateway = FakeGateway({})
    result = ocr_pdf(source_pdf, tmp_path / "book.txt", PDFOCROptions("gpt-fixture", "high"),
                     gateway=gateway, checkpoint_dir=checkpoints, _runtime=runtime())
    assert result.success and not gateway.calls
    assert "page 3" in result.text


def test_selected_failures_do_not_stop_other_pages_or_publish_complete_result(source_pdf):
    gateway = FakeGateway({1: "one", 3: RuntimeError("upstream failed"), 5: "five"})
    progress = []
    result = ocr_pdf_pages(source_pdf, [5, 3, 1], PDFOCROptions("gpt-fixture"),
                           gateway=gateway, progress_callback=progress.append, _runtime=runtime())
    assert pages_called(gateway) == [1, 3, 5]
    assert result.texts_by_page == {1: "one", 5: "five"}
    assert result.errors_by_page == {3: "upstream failed"}
    assert result.sources_by_page[1]["model"] == "gpt-fixture"
    assert progress[-1].status == "partial"


def test_explicit_retry_uses_original_pdf_identity_and_records_fallback_source(source_pdf, tmp_path):
    identity = build_pdf_ocr_run_identity(source_pdf, "gpt-fixture", "high")
    plan = PDFOCRRetryPlan(identity.fingerprint, (5,))
    gateway = FakeGateway({5: "retry"})
    result = ocr_pdf_pages(source_pdf, [5], PDFOCROptions("gpt-fixture", "high", fast_mode=True),
                           gateway=gateway, checkpoint_dir=tmp_path / "pages", retry_plan=plan,
                           _runtime=runtime())
    assert pages_called(gateway) == [5]
    assert gateway.calls[0]["model"] == "gemini-3.5-flash-lite"
    assert "service_tier" not in gateway.calls[0]
    assert result.fallback_page_models == {5: "gemini-3.5-flash-lite"}
    assert result.sources_by_page[5]["fallback"] is True
    assert result.sources_by_page[5]["primary_model"] == "gpt-fixture"


def test_stop_preserves_inflight_page_then_resume_replays_checkpoint(source_pdf, tmp_path):
    checkpoints = tmp_path / "pages"
    stop = Event()
    gateway = FakeGateway({1: "one"}, stop_event=stop)
    delivered = []
    first = ocr_pdf_pages(source_pdf, [1, 5], PDFOCROptions("gpt-fixture"), gateway=gateway,
                          checkpoint_dir=checkpoints, stop_event=stop, _runtime=runtime(),
                          page_callback=lambda *args: delivered.append(args))
    assert first.interrupted and first.texts_by_page == {1: "one"}
    assert first.errors_by_page == {} and pages_called(gateway) == [1]
    assert (checkpoints / "page-000001.txt").read_text() == "one"

    gateway = FakeGateway({5: "five"})
    resumed = ocr_pdf_pages(source_pdf, [1, 5], PDFOCROptions("gpt-fixture"), gateway=gateway,
                            checkpoint_dir=checkpoints, _runtime=runtime(),
                            page_callback=lambda *args: delivered.append(args))
    assert not resumed.interrupted and pages_called(gateway) == [5]
    assert resumed.texts_by_page == {1: "one", 5: "five"}
    assert [item[0] for item in delivered] == [1, 1, 5]


def test_callback_failure_keeps_checkpoint_for_later_replay_without_new_ocr(source_pdf, tmp_path):
    checkpoints = tmp_path / "pages"
    gateway = FakeGateway({3: "recognized"})

    def failed_write(_page, _text, _source):
        raise RuntimeError("database write failed")

    first = ocr_pdf_pages(source_pdf, [3], PDFOCROptions("gpt-fixture"), gateway=gateway,
                          checkpoint_dir=checkpoints, page_callback=failed_write, _runtime=runtime())
    assert first.texts_by_page == {} and first.errors_by_page == {3: "database write failed"}
    assert (checkpoints / "page-000003.txt").read_text() == "recognized"
    replayed = []
    gateway = FakeGateway({})
    resumed = ocr_pdf_pages(source_pdf, [3], PDFOCROptions("gpt-fixture"), gateway=gateway,
                            checkpoint_dir=checkpoints, _runtime=runtime(),
                            page_callback=lambda *args: replayed.append(args))
    assert not gateway.calls and resumed.texts_by_page == {3: "recognized"}
    assert replayed[0][0:2] == (3, "recognized")


def test_selected_ocr_requires_completed_sse_without_changing_generic_gateway(source_pdf, tmp_path, monkeypatch):
    from src.llm.gateway import GatewayClient, GatewayConnection

    gateway = GatewayClient(GatewayConnection("http://127.0.0.1:1234", "fixture"))
    monkeypatch.setattr("src.llm.gateway.read_http_response", lambda *args, **kwargs:
                        'data: {"type":"response.output_text.delta","delta":"部分文字"}\n\n')
    result = ocr_pdf_pages(source_pdf, [2], PDFOCROptions("fixture"), gateway=gateway,
                           checkpoint_dir=tmp_path / "pages", _runtime=runtime())
    assert result.texts_by_page == {} and 2 in result.errors_by_page
    assert not (tmp_path / "pages/page-000002.txt").exists()
    assert gateway.require_stream_completed is False


def test_stop_interrupts_stagger_wait_and_drains_inflight():
    release = Event()

    class StopOnWait:
        stopped = False
        wait_calls = 0

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            assert timeout > 0
            self.wait_calls += 1
            self.stopped = True
            release.set()
            return True

    stop = StopOnWait()
    started = []

    def worker(task):
        started.append(task.page_number)
        assert release.wait(2)
        return "saved"

    result = run_staggered_page_ocr_tasks([OCRPageTask(1), OCRPageTask(2)], worker,
                                           max_concurrency=2, submit_interval_seconds=30,
                                           stop_event=stop)
    assert stop.wait_calls == 1 and started == [1]
    assert result.interrupted and result.texts_by_page == {1: "saved"}


def test_original_whole_book_uses_existing_checkpoint_runtime_seam(source_pdf, tmp_path):
    called = []
    gateway = FakeGateway({})

    def old_loader(directory, page_count):
        called.append((directory, page_count))
        return {page: f"page {page}" for page in range(1, page_count + 1)}

    result = ocr_pdf(source_pdf, tmp_path / "output.txt", PDFOCROptions("gpt-fixture"),
                     gateway=gateway, checkpoint_dir=tmp_path / "pages",
                     _runtime=PDFOCRRuntime(load_checkpoints=old_loader))
    assert result.success and called == [(tmp_path / "pages", 5)]
    assert not gateway.calls

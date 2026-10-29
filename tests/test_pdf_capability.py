"""独立 PDF 合同：无 LoadedSettings、job、Web，且成功必须包含产物发布。"""
from dataclasses import replace
from pathlib import Path

from src.pdf import PDFOCROptions, build_pdf_ocr_run_identity, ocr_pdf
from src.pdf.ocr import PDFOCRRuntime
from src.web.tasks import serialize_reference_file_progress
from src.reference_utils import ReferenceFileProgress


class FixedGateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def responses_stream_text(self, payload):
        self.calls.append(payload)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def runtime(pages=3):
    return PDFOCRRuntime(page_count=lambda path: pages,
                         render_page=lambda path, page, count: f"data:image/png;base64,page-{page}")


def test_independent_pdf_partial_resume_blank_page_and_publication(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"fixture PDF")
    output = tmp_path / "book.txt"
    checkpoints = tmp_path / "pages"
    options = PDFOCROptions("gpt-fixture", "high")
    gateway = FixedGateway(["第一页。", RuntimeError("content_filter"), ""])
    first = ocr_pdf(source, output, options, gateway=gateway,
                    checkpoint_dir=checkpoints, _runtime=runtime())
    assert first.status == "partial" and not first.success
    assert first.state.completed_page_numbers == (1, 3)
    assert first.state.failed_page_numbers == (2,)
    assert not output.exists()
    assert (checkpoints / "page-000003.txt").read_text() == ""

    resumed_gateway = FixedGateway(["第二页。"])
    progress = []
    resumed = ocr_pdf(source, output, options, gateway=resumed_gateway,
                      checkpoint_dir=checkpoints, progress_callback=progress.append, _runtime=runtime())
    assert resumed.status == "success" and resumed.success
    assert len(resumed_gateway.calls) == 1
    assert resumed_gateway.calls[0]["input"][0]["content"][1]["image_url"].endswith("page-2")
    assert output.read_text() == "第一页。\n\n----- OCR_PAGE_BREAK: 1 -> 2 -----\n\n第二页。\n\n----- OCR_PAGE_BREAK: 2 -> 3 -----"
    assert progress[-1].status == "success"
    assert all(p.status == "running" for p in progress[:-1])


def test_all_pages_complete_but_publish_failed_is_not_success(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fixture")
    # 一个目录占据目标文件名，原子发布必须失败。
    output = tmp_path / "book.txt"
    output.mkdir()
    result = ocr_pdf(source, output, PDFOCROptions("fixture"),
                     gateway=FixedGateway([""]), _runtime=runtime(1))
    assert result.status == "failed" and result.error is not None
    assert result.state.completed_pages == 1
    assert result.state.resumable is False
    assert result.state.status == "failed"


def test_reference_progress_does_not_infer_success_from_resumable(tmp_path):
    progress = ReferenceFileProgress(tmp_path / "x.pdf", tmp_path / "x.txt", 1, 1, (), {}, False)
    assert serialize_reference_file_progress(progress)["success"] is False
    assert serialize_reference_file_progress(replace(progress, status="failed"))["success"] is False
    assert serialize_reference_file_progress(replace(progress, status="success"))["success"] is True


def test_identity_uses_only_existing_source_model_reasoning_contract(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fixture")
    assert build_pdf_ocr_run_identity(source, "gpt-fixture", " high ") == build_pdf_ocr_run_identity(source, "gpt-fixture", "high")


def test_pdf_enforces_completed_even_with_default_generic_gateway(tmp_path, monkeypatch):
    from src.llm.gateway import GatewayClient, GatewayConnection
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fixture")
    gateway = GatewayClient(GatewayConnection("http://127.0.0.1:1234", "fixture"))
    assert gateway.require_stream_completed is False
    # 通用客户端默认兼容SSE省略completed；PDF操作不能继承这个宽松默认。
    monkeypatch.setattr("src.llm.gateway.read_http_response", lambda *args, **kwargs:
                        'data: {"type":"response.output_text.delta","delta":"部分文字"}\n\n')
    result = ocr_pdf(source, tmp_path / "book.txt", PDFOCROptions("fixture"), gateway=gateway,
                     checkpoint_dir=tmp_path / "pages", _runtime=runtime(1))
    assert result.status == "partial"
    assert result.state.completed_pages == 0
    assert not (tmp_path / "pages/page-000001.txt").exists()
    assert not (tmp_path / "book.txt").exists()
    assert gateway.require_stream_completed is False

"""离线验收：真实 PDF 渲染/恢复，以及固定模型响应下的产品编排和导出。

只创建合成材料，不读取用户输入或凭据、不下载模型、不发远端请求。
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from src.config_loader import load_settings
from src.llm.gateway import GatewayConnection, ProxySettings
from src.pdf import (
    PDFOCROptions, build_pdf_ocr_checkpoint_namespace, build_pdf_ocr_retry_plan,
    build_pdf_ocr_run_identity, ocr_pdf,
)
from src.pdf.ocr import render_pdf_page_as_png_data_url
from src.pipeline import run_stage
from tests.helpers import write_minimal_settings


class FixedOCRGateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def responses_stream_text(self, payload):
        self.requests.append(payload)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def synthetic_pdf(path: Path) -> None:
    writer = PdfWriter()
    for number in range(1, 4):
        page = writer.add_blank_page(width=300, height=180)
        if number == 3:
            continue
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 18 Tf 30 90 Td (Synthetic page {number}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)


def validate(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="offline-", dir=output_root))
    source = root / "synthetic.pdf"
    synthetic_pdf(source)
    output = root / "pdf" / "book.txt"
    options = PDFOCROptions("gpt-fixture", "high", max_concurrency=1)
    identity = build_pdf_ocr_run_identity(source, options.model, options.reasoning_effort)
    checkpoints = build_pdf_ocr_checkpoint_namespace(root / "pdf" / "pages", identity)
    first_gateway = FixedOCRGateway(["Synthetic page 1", RuntimeError("content_filter"), ""])
    first = ocr_pdf(source, output, options, gateway=first_gateway, checkpoint_dir=checkpoints)
    assert first.status == "partial" and first.state.completed_page_numbers == (1, 3)
    assert not output.exists()
    plan = build_pdf_ocr_retry_plan(identity, first.state.page_errors)
    retry_gateway = FixedOCRGateway(["Synthetic page 2"])
    resumed = ocr_pdf(source, output, options, gateway=retry_gateway, checkpoint_dir=checkpoints,
                      retry_plan=plan)
    assert resumed.status == "success" and len(retry_gateway.requests) == 1
    assert retry_gateway.requests[0]["model"] == "gemini-3.5-flash-lite"
    cached_gateway = FixedOCRGateway([])
    cached = ocr_pdf(source, root / "pdf" / "remerged.txt", options,
                     gateway=cached_gateway, checkpoint_dir=checkpoints)
    assert cached.status == "success" and not cached_gateway.requests
    assert cached.text == output.read_text(encoding="utf-8")
    assert cached.text.count("----- OCR_PAGE_BREAK:") == 2
    assert (checkpoints / "page-000003.txt").read_text(encoding="utf-8") == ""
    image = render_pdf_page_as_png_data_url(source, 1, 3)
    (root / "rendered-page-1.png").write_bytes(base64.b64decode(image.split(",", 1)[1]))

    # 产品编排使用真实 prepare/refine/export 操作；只固定不可控模型响应。
    project = root / "product"
    config = write_minimal_settings(project, llm_overrides={"backends": ["codex_api"]})
    loaded = load_settings(settings_path=config, project_root=project)
    reference = loaded.path_for("reference_dir") / "demo.txt"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("这是合成参考原文。", encoding="utf-8")
    asr = loaded.path_for("asr_dir") / "demo.txt"
    asr.parent.mkdir(parents=True, exist_ok=True)
    asr.write_text("这是合成参考原文。\n接下来讨论这个句子。", encoding="utf-8")
    # 显式本地连接仅用于验证参数传递，模型方法被固定响应替换，不发 HTTP。
    connection = GatewayConnection("http://127.0.0.1:1", "offline-fixture", ProxySettings())
    fixed_markdown = "# 合成整理稿\n\n> 这是合成参考原文。\n\n接下来讨论这个句子。"
    requests = []

    def fixed_response(self, payload, **kwargs):
        assert self.connection is connection
        requests.append(payload)
        return json.dumps({"final_markdown": fixed_markdown}, ensure_ascii=False)

    exits = {}
    logger = logging.getLogger("backend-module-validation")
    with patch("src.llm.gateway.CodexLBClient.codex_responses_text", fixed_response):
        for stage in ("prepare-reference", "refine", "export-markdown"):
            exits[stage] = run_stage(stage, loaded, logger, gateway_connection=connection)
            assert exits[stage] == 0, (stage, exits)
    final_md = loaded.path_for("final_dir") / "demo.md"
    final_txt = loaded.path_for("final_dir") / "demo.txt"
    assert final_md.read_text(encoding="utf-8").strip() == fixed_markdown
    assert "这是合成参考原文。" in final_txt.read_text(encoding="utf-8")
    assert requests
    evidence = {
        "model_boundary": "固定本地模型响应；未执行 OCR/LLM 真实模型或 ASR 推理",
        "remote_requests": 0,
        "pdf": {"first_status": first.status, "first_completed": first.state.completed_pages,
                "resume_status": resumed.status, "resume_model_requests": len(retry_gateway.requests),
                "resume_model": retry_gateway.requests[0]["model"], "cached_model_requests": 0,
                "pages": cached.state.page_count, "blank_pages": [3], "page_breaks": 2,
                "output": str(output), "checkpoints": str(checkpoints)},
        "product": {"stage_exit_codes": exits, "fixed_model_requests": len(requests),
                    "markdown": str(final_md), "text": str(final_txt)},
    }
    (root / "summary.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp/backend-modules-validation")
    args = parser.parse_args()
    print(f"Evidence: {validate(args.output.resolve())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""离线组装器验收：复制真实历史资料，增加一个缺页 PDF，禁止实际模型调用。

    .venv/bin/python tests/library_composer_server.py --data-dir /tmp/新的空目录 \\
        --source-root "$PWD" --port 18769

保留的文件都写入 --data-dir；不能复用已有目录或生产数据目录。浏览器回归：
    LIBRARY_TEST_URL=http://127.0.0.1:18769 LIBRARY_TEST_OUTPUT=/tmp/输出 \\
        node frontend/tests/library-composer.mjs
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from pypdf import PdfWriter
from fastapi.responses import JSONResponse

from tests.library_acceptance_server import build_app


class BrowserHarness:
    """只模拟上传和任务投递；业务数据仍走真实书库，绝不执行模型。"""

    def __init__(self, app, fixtures):
        self.app = app
        self.fixtures = fixtures
        self.last_job = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path, method = scope["path"], scope["method"]
            if path == "/api/__library_test__" and method == "GET":
                response = JSONResponse({"isolated": True, "model_execution": False, **self.fixtures})
            elif path == "/api/__workbench_test__" and method == "GET":
                response = JSONResponse({"isolated": True, "model_execution": False})
            elif path == "/api/__last_job__" and method == "GET":
                response = JSONResponse(self.last_job)
            elif path == "/api/frontend-settings" and method == "GET":
                response = JSONResponse({"ocr_model": "fixture-model", "ocr_backend": "codex_api",
                    "ocr_reasoning_effort": "medium", "ocr_fast_mode": False,
                    "ocr_max_concurrency": 2, "ocr_submit_interval_seconds": 0,
                    "profile": "local_cpu", "asr_candidate": "whisper-existing",
                    "secondary_asr_candidate": "", "fast_mode": False})
            elif path == "/api/uploads" and method == "POST":
                response = JSONResponse({"kind": "video", "name": "sample.mp4", "path": "/fixture/sample.mp4",
                                         "directory": "/fixture", "size": 9})
            elif path == "/api/jobs" and method == "POST":
                from starlette.requests import Request
                self.last_job = await Request(scope, receive).json()
                response = JSONResponse({"job_id": "fixture-job"}, status_code=202)
            elif path == "/api/jobs/fixture-job" and method == "GET":
                response = JSONResponse({"id": "fixture-job", "kind": "job", "status": "pending",
                    "created_at": "", "updated_at": "", "current_stage": "pending", "error_message": "", "output_path": ""})
            else:
                return await self.app(scope, receive, send)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_composer_app(root: Path, source_root: Path):
    # build_app rejects non-empty directories, copies both actual history tasks,
    # and installs OfflineOnly: mutations cannot reach a model/ASR endpoint.
    app = build_app(root, source_root)
    fixtures = {}
    for name, pages in (("A", 2), ("B", 1)):
        path = root / f"{name}.pdf"
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=300, height=400)
        with path.open("wb") as handle:
            writer.write(handle)
        fixtures[f"pdf_{name.lower()}"] = str(path)
    app.add_middleware(BrowserHarness, fixtures=fixtures)
    fixture = root / "fixture-missing.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=300, height=400)
    with fixture.open("wb") as handle:
        writer.write(handle)
    fixtures["pdf_missing"] = str(fixture)
    book = app.state.library.add_book(fixture, filename="缺页测试书.pdf")
    app.state.library.save_page(book["id"], 1, text="", base_revision_id=None,
                                note="离线验收：空白页已确认")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18769)
    args = parser.parse_args()
    uvicorn.run(build_composer_app(args.data_dir, args.source_root), host="127.0.0.1", port=args.port)

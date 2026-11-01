"""离线书库验收服务：只复制两份真实历史资料，不执行识别或模型任务。

用项目 .venv 启动，--data-dir 必须指向新建或空目录。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi.responses import JSONResponse
from pypdf import PdfReader

from api_server import create_app
from tests.helpers import write_minimal_settings


TASKS = (
    "pdf-ocr-81c840372047460c8d349f846d6bec5a",
    "pdf-ocr-d4a2b14d7b524b44b14df6f9bc1cfe72",
)
BEFORE_MARKER = "〔离线验收人工修订〕"
AFTER_MARKER = "〔离线验收发布后更新〕"


def _copy_history(root: Path, source_root: Path, task_id: str) -> dict:
    task = source_root / "data/jobs/pdf-ocr" / task_id
    state = json.loads((task / "state.json").read_text(encoding="utf-8"))
    raw_input = (state.get("request_payload") or {}).get("input_path")
    if not isinstance(raw_input, str):
        raise ValueError(f"历史任务缺少输入路径：{task_id}")
    raw_path = PurePosixPath(raw_input)
    if ".." in raw_path.parts or not raw_path.is_relative_to("/app/data/uploads/pdf-ocr"):
        raise ValueError(f"历史任务输入不在容器 PDF 上传目录：{task_id}")
    relative_data = raw_path.relative_to("/app/data")
    upload_root = (source_root / "data/uploads/pdf-ocr").resolve()
    original_pdf = (source_root / "data" / relative_data).resolve()
    if not original_pdf.is_relative_to(upload_root) or not original_pdf.is_file() or original_pdf.suffix.lower() != ".pdf":
        raise ValueError(f"历史任务原 PDF 不存在或不在上传目录：{task_id}")

    copy_pdf = root / "data" / relative_data
    copy_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original_pdf, copy_pdf)
    copy_task = root / "data/jobs/pdf-ocr" / task_id
    copy_task.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task, copy_task)
    return {"task_id": task_id, "container_input": raw_input, "original_pdf": str(original_pdf),
            "isolated_upload_pdf": str(copy_pdf), "original_task": str(task),
            "isolated_task": str(copy_task)}


class OfflineOnly:
    """允许书库管理；任何模型执行入口及 WebSocket 均不可用。"""

    _allowed = (
        ("POST", re.compile(r"/api/library/books/?")),
        ("PATCH", re.compile(r"/api/library/books/[^/]+/?")),
        ("DELETE", re.compile(r"/api/library/books/[^/]+/?")),
        ("PUT", re.compile(r"/api/library/books/[^/]+/pages/[^/]+/text/?")),
        ("POST", re.compile(r"/api/library/materials/?")),
        ("PATCH", re.compile(r"/api/library/materials/[^/]+/?")),
        ("DELETE", re.compile(r"/api/library/materials/[^/]+/?")),
        ("POST", re.compile(r"/api/library/materials/[^/]+/revisions/?")),
        ("POST", re.compile(r"/api/library/imports/?")),
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http":
            path, method = scope["path"], scope["method"]
            if method == "GET" and path == "/api/__library_test__":
                response = JSONResponse({"isolated": True, "model_execution": False})
                await response(scope, receive, send)
                return
            if method not in {"GET", "HEAD", "OPTIONS"} and not any(
                method == allowed_method and pattern.fullmatch(path)
                for allowed_method, pattern in self._allowed
            ):
                response = JSONResponse({"detail": "离线验收服务不执行 OCR 或模型任务。"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(root: Path, source_root: Path):
    root, source_root = Path(root).resolve(), Path(source_root).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("--data-dir 必须是新建或空目录，不能复用已有数据。")
    root.mkdir(parents=True, exist_ok=True)
    write_minimal_settings(root)
    history = [_copy_history(root, source_root, task_id) for task_id in TASKS]

    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "dist").symlink_to(source_root / "frontend/dist", target_is_directory=True)
    app = create_app(project_root=root)
    app.add_middleware(OfflineOnly)
    service = app.state.library

    candidates = service.history_candidates()
    selected = []
    for task_id in TASKS:
        found = [candidate for candidate in candidates["items"] if candidate["task_id"] == task_id]
        if len(found) != 1:
            raise ValueError(f"验收任务应有一个可导入书籍：{task_id}")
        selected.append(found[0])
    if [candidate["reusable_pages"] for candidate in selected] != [9, 1]:
        raise ValueError("历史页身份校验未得到预期的 9 页与 1 页，请查看候选报告。")

    report = service.import_history([candidate["id"] for candidate in selected])
    by_candidate = {item["id"]: item for item in report["items"]}
    for record, candidate in zip(history, selected):
        imported = by_candidate[candidate["id"]]
        if imported["status"] != "success" or imported["imported_pages"] != candidate["reusable_pages"]:
            raise ValueError(f"历史导入未成功：{imported}")
        record.update(candidate=candidate, import_result=imported, book_id=imported["book_id"])

    book_a, book_b = (record["book_id"] for record in history)
    page1 = service.get_page(book_a, 1)
    if page1["text"] is None:
        raise ValueError("书 A 第 1 页历史文字未导入。")
    revised1 = service.save_page(book_a, 1, text=page1["text"] + "\n" + BEFORE_MARKER,
                                 base_revision_id=page1["current_revision_id"], note="离线验收人工修订")
    ranges = [
        {"book_id": book_a, "start_page": 1, "end_page": 2},
        {"book_id": book_a, "start_page": 4, "end_page": 4},
        {"book_id": book_b, "start_page": 1, "end_page": 1},
    ]
    material = service.create_material("真实两书接线验收", ranges)
    if material["page_count"] != 4 or material["missing_pages"]:
        raise ValueError("验收材料的四页未全部就绪。")
    revision = service.publish_material(material["id"], base_version=material["version"])
    files = {kind: service.revision_file(revision["id"], kind) for kind in ("pdf", "txt", "sources")}
    before = {kind: path.read_bytes() for kind, path in files.items()}
    sources = json.loads(before["sources"])
    if len(PdfReader(files["pdf"]).pages) != 4:
        raise ValueError("验收组合 PDF 不是四页。")
    expected = [(book_a, 1), (book_a, 2), (book_a, 4), (book_b, 1)]
    if [(page["book_id"], page["pdf_page"]) for page in sources["pages"]] != expected:
        raise ValueError("验收材料的来源页顺序不一致。")
    if sources["pages"][0]["text_revision_id"] != revised1["current_revision_id"]:
        raise ValueError("验收材料未固定书 A 第 1 页人工修订。")

    page2 = service.get_page(book_a, 2)
    if page2["text"] is None:
        raise ValueError("书 A 第 2 页历史文字未导入。")
    revised2 = service.save_page(book_a, 2, text=page2["text"] + "\n" + AFTER_MARKER,
                                 base_revision_id=page2["current_revision_id"], note="离线验收发布后更新")
    revision = service.get_material_revision(revision["id"])
    if not revision["has_updates"] or any(path.read_bytes() != before[kind] for kind, path in files.items()):
        raise ValueError("发布后修订未显示更新，或已发布材料发生变化。")

    books = {record["book_id"]: record for record in history}
    page_map = []
    for page in sources["pages"]:
        record = books[page["book_id"]]
        page_map.append({**page, "task_id": record["task_id"], "original_pdf": record["original_pdf"],
                         "isolated_upload_pdf": record["isolated_upload_pdf"],
                         "library_pdf": str(root / "data/library/books" / page["book_id"] / "source.pdf")})
    evidence = {
        "isolated": True, "model_execution": False, "data_dir": str(root), "source_root": str(source_root),
        "history": history, "import_report": report, "book_ids": {"a": book_a, "b": book_b},
        "material": material, "revision": revision, "revision_files": {k: str(v) for k, v in files.items()},
        "pages": page_map,
        "manual_edits": {
            "before_publish": {"book_id": book_a, "pdf_page": 1, "revision_id": revised1["current_revision_id"], "marker": BEFORE_MARKER},
            "after_publish": {"book_id": book_a, "pdf_page": 2, "revision_id": revised2["current_revision_id"], "marker": AFTER_MARKER},
        },
    }
    (root / "acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(build_app(args.data_dir, args.source_root), host="127.0.0.1", port=args.port)

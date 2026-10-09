"""Isolated real API for frontend browser checks; never runs ASR or model calls.

Run with the project .venv, and point --data-dir at a new scratch directory.
The fixture records are UI examples, not evidence of model execution.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi.responses import JSONResponse

from api_server import create_app
from src.web.state_store import create_initial_state, write_json_file
from tests.helpers import write_minimal_settings


def build_app(root: Path):
    if root.exists() and any(root.iterdir()):
        raise ValueError("--data-dir must be new or empty; existing data is never reused")
    root.mkdir(parents=True, exist_ok=True)
    write_minimal_settings(root)
    app = create_app(project_root=root)
    for status in ("success", "failed", "running"):
        identifier = f"ui-example-{status}"
        job_dir = root / "data/jobs" / identifier
        state = create_initial_state(identifier, "job")
        state.update(
            status=status,
            current_stage="done" if status == "success" else "refine",
            completed_stages=["extract-audio", "transcribe", "prepare-reference"],
            input_summary={
                "book_name": "读书会界面验证样例",
                "chapter": {"success": "第一章", "failed": "第二章", "running": "第三章"}[status],
                "video_source": "/example/读书会/用于验证长路径换行与操作区域布局的录屏资料/本次讨论.mp4",
                "content_type": "book_club",
            },
            error_message="示例错误：连接暂时不可用，请检查运行设置。" if status == "failed" else "",
        )
        if status == "success":
            output = job_dir / "output/final/source.md"
            output.parent.mkdir(parents=True)
            output.write_text("# 界面验证样例\n\n这是用于验证下载和预览的样例文本。\n", encoding="utf-8")
            state["output_path"] = str(output)
            write_json_file(job_dir / "intermediate/refined/source.json", {"final_markdown": output.read_text(encoding="utf-8")})
        if status == "running":
            app.state.active_jobs.add(identifier)
        write_json_file(job_dir / "state.json", state)

    @app.middleware("http")
    async def protect_model_execution(request, call_next):
        path = request.url.path
        if path == "/api/__workbench_test__" and request.method == "GET":
            return JSONResponse({"isolated": True, "model_execution": False, "data_dir": str(root)})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = (
                path == "/api/frontend-settings"
                or path == "/api/uploads"
                or path.startswith("/api/stage-inputs/")
                or path == "/api/stages/export-markdown/file-run"
                or (request.method == "DELETE" and path.startswith("/api/jobs/ui-example-"))
            )
            if not allowed:
                return JSONResponse({"detail": "UI test server: model execution is disabled"}, status_code=403)
        return await call_next(request)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(build_app(args.data_dir.resolve()), host="127.0.0.1", port=args.port)

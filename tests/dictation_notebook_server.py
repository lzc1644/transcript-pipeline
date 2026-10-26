"""Isolated notebook API fixture. No recording, worker or external model calls."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from uuid import uuid4

import uvicorn
from fastapi import Response

from api_server import create_app
from src.dictation import document
from src.dictation.service import now
from src.dictation.storage import public_state


def build_app(root):
    root.mkdir(parents=True, exist_ok=False)
    (root / "config").mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "config/settings.yaml", root / "config/settings.yaml")
    app = create_app(project_root=root)
    service = app.state.dictation
    def cleaner(*args):
        def clean(text, *args):
            return {"text": text + "。", "needs_review": False}
        clean.document = clean
        return clean
    service.cleanup_factory = cleaner

    @app.post("/api/notebook-fixture/seed")
    async def seed(response: Response, legacy: bool = False):
        sid = uuid4().hex
        text = "文章😀开头。\n" + "\n".join(f"第{i}段，预算1500元，不是15000元。等待确认后再安排活动。" for i in range(70))
        state = dict(id=sid, owner="notebook-fixture", status="completed", phase="已停止，处理完成", mode="live",
            created_at=now(), revision=0, event_sequence=0, samples=0, next_sequence=0, utterances=[], metrics={},
            config={"terms": [], "cleanup": True, "cleanup_model": "gpt-6-luna"},
            worker_config={"cleanup_model": "gpt-6-luna", "cleanup_timeout_seconds": 3600, "terms": []}, gateway={}, document_text=text)
        if not legacy:
            document.enable(state, service.storage.render_draft)
        service.storage.create(state)
        service.states[sid] = state
        service.projections[sid] = text
        response.set_cookie("dictation_owner", "notebook-fixture", httponly=True, samesite="strict")
        return public_state(state)

    @app.post("/api/notebook-fixture/{sid}/speech")
    async def speech(sid: str):
        state = service.get(sid, "notebook-fixture")
        u = dict(id=str(len(state["utterances"]) + 1), text="语音落字。", raw_text="语音落字。", revision=1,
                 start_sample=0, end_sample=100, edited=False, status="raw", needs_review=False, preview="")
        state["utterances"].append(u)
        service.commit_document(state, lambda candidate: document.insert_utterance(candidate, u), event="asr.final")
        return public_state(state)

    @app.post("/api/notebook-fixture/{sid}/clean")
    async def clean(sid: str):
        state = service.get(sid, "notebook-fixture")
        u = state["utterances"][-1]
        await service.clean_utterance(state, u["id"], u["revision"])
        return public_state(state)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8027)
    args = parser.parse_args()
    uvicorn.run(build_app(args.root.resolve()), host="127.0.0.1", port=args.port)

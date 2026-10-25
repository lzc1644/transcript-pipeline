"""Isolated HTTP/WS protocol fixture. No ASR, LLM, credentials or user history."""
import argparse
import json
from pathlib import Path
import uuid

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8031)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    app = FastAPI()
    states = {}
    stats = {"fixture": True, "creates": 0, "packets": 0, "samples": 0, "stop_samples": [], "sessions": {}}

    def save():
        (args.root / "protocol.json").write_text(json.dumps(stats, indent=2))

    @app.get("/fixture-target", response_class=HTMLResponse)
    def target():
        return '<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><title>扩展采音验证目标页</title><style>body{font:18px system-ui;max-width:760px;margin:60px auto;padding:20px}textarea{width:100%;min-height:220px;font:inherit;padding:12px;box-sizing:border-box}</style><h1>保持这里的输入焦点</h1><p>隔离录音通路验证，不调用模型，不提交表单。</p><textarea autofocus>目标输入框：这段文字不应被 S1 录音控制修改。</textarea></html>'

    @app.get("/api/health")
    def health():
        return {"status": "ok", "fixture": True}

    @app.get("/api/dictation/config")
    def config():
        return {"cleanup_model": "protocol-fixture", "max_upload_mb": 512, "max_upload_minutes": 240}

    @app.get("/api/dictation/sessions")
    def sessions():
        return list(states.values())

    @app.post("/api/dictation/sessions")
    def create():
        stats["creates"] += 1
        sid = uuid.uuid4().hex
        state = states[sid] = {"id": sid, "status": "listening", "phase": "正在听写（协议夹具）", "mode": "live",
            "revision": 1, "event_sequence": 1, "next_sequence": 0, "samples": 0, "created_at": "2026-10-24T00:00:00Z",
            "config": {"cleanup": True, "terms": [], "cleanup_model": "protocol-fixture"}, "utterances": [], "draft_text": ""}
        stats["sessions"][sid] = {"packets": [], "samples": 0, "duplicates": 0, "errors": [], "stop_samples": []}
        save()
        return state

    @app.get("/api/dictation/sessions/{sid}")
    def state(sid: str):
        return states[sid]

    @app.post("/api/dictation/sessions/{sid}/{action}")
    def control(sid: str, action: str):
        states[sid].update(status="cancelled" if action == "cancel" else "completed")
        return states[sid]

    @app.websocket("/api/dictation/sessions/{sid}/stream")
    async def audio(ws: WebSocket, sid: str):
        await ws.accept()
        state = states[sid]
        await ws.send_json({"type": "session.snapshot", "snapshot": state})
        try:
            while True:
                item = await ws.receive()
                if item["type"] == "websocket.disconnect":
                    return
                if item.get("bytes"):
                    packet = item["bytes"]
                    seq, pos = int.from_bytes(packet[:4], "little"), int.from_bytes(packet[4:12], "little")
                    metrics = stats["sessions"][sid]
                    if seq < state["next_sequence"]:
                        metrics["duplicates"] += 1
                        await ws.send_json({"type": "audio.ack", "next_sequence": state["next_sequence"]})
                        save()
                        continue
                    if seq != state["next_sequence"] or pos != state["samples"]:
                        metrics["errors"].append({"seq": seq, "pos": pos, "expected_seq": state["next_sequence"], "expected_pos": state["samples"]})
                        save()
                        await ws.close(code=1008)
                        return
                    count = (len(packet) - 12) // 2
                    metrics["packets"].append({"seq": seq, "pos": pos, "samples": count})
                    metrics["samples"] += count
                    with (args.root / f"{sid}.pcm").open("ab") as output:
                        output.write(packet[12:])
                    state["next_sequence"] = seq + 1
                    state["samples"] = pos + count
                    stats["packets"] += 1
                    stats["samples"] += (len(packet) - 12) // 2
                    stats["last_packet_samples"] = (len(packet) - 12) // 2
                    await ws.send_json({"type": "audio.ack", "next_sequence": state["next_sequence"]})
                elif item.get("text"):
                    command = json.loads(item["text"])
                    if command["type"] == "stop":
                        state.update(status="completed", phase="协议夹具已结束", revision=state["revision"] + 1)
                        stats["stop_samples"].append(state["samples"])
                        stats["sessions"][sid]["stop_samples"].append(state["samples"])
                        await ws.send_json({"type": "session.snapshot", "snapshot": state})
                save()
        except Exception:
            save()
            raise

    frontend = Path(__file__).resolve().parents[3] / "frontend/dist"

    class SPA(StaticFiles):
        async def get_response(self, path, scope):
            return await super().get_response(path if (frontend / path).is_file() else "index.html", scope)

    app.mount("/", SPA(directory=frontend), name="frontend")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

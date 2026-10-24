from __future__ import annotations

import asyncio
import base64
import fcntl
import json
from pathlib import Path
import struct
import time

import pytest
from fastapi.testclient import TestClient

from tests.test_dictation import root, fake_cleanup
from src.dictation.schemas import CleanupRequest, SessionRequest
from src.dictation.service import DictationService


class FakeWorker:
    started = []
    def __init__(self, interpreter, mode, config, root, log_path, lock_fd=None):
        self.mode = mode
        self.events = asyncio.Queue()
        self.samples = 0
        self.closed = False
    async def start(self):
        self.started.append(self.mode)
    async def send(self, value):
        if self.mode == "final":
            await self.events.put({"type": "final", "text": "不是不能做，是现在不做", "inference_seconds": .01})
        elif value["op"] == "audio":
            if not self.samples:
                await self.events.put({"type": "utterance.start", "utterance_id": "1", "start": 0})
            self.samples += len(base64.b64decode(value["pcm"])) // 2
            await self.events.put({"type": "processed", "samples": self.samples, "last_voice_sample": self.samples})
        elif value["op"] == "stop":
            if self.samples:
                await self.events.put({"type": "utterance.end", "utterance_id": "1", "start": 0,
                    "end": self.samples, "last_voice": self.samples, "boundary": "stop", "preview": "预览"})
            await self.events.put({"type": "drained"})
    async def read(self):
        return await self.events.get()
    async def close(self):
        self.closed = True


async def ready(state):
    for _ in range(1000):
        if state["status"] not in {"created", "preparing"}:
            return
        await asyncio.sleep(.001)
    raise AssertionError("not ready")


def test_stop_drains_tail_releases_gpu_and_preserves_separate_outputs(root):
    async def run():
        service = DictationService(root, worker_factory=FakeWorker, cleanup_factory=fake_cleanup)
        state = await service.create("owner", SessionRequest())
        await ready(state)
        runtime = service.runtimes[state["id"]]
        await runtime.accept_audio(struct.pack("<IQ", 0, 0) + b"\x01\x00" * 997)
        await runtime.stop()
        await asyncio.wait_for(runtime.task, 2)
        assert state["status"] == "completed"
        assert state["utterances"][0]["end_sample"] == 997
        assert state["utterances"][0]["status"] == "cleaned"
        directory = service.storage.path(state["id"])
        assert (directory / "asr/1.json").is_file() and (directory / "cleaned/1.json").is_file()
        assert (directory / "draft.txt").read_text().endswith("。")
        assert runtime.lock_file is None and runtime.cpu.closed and runtime.final.closed
        await service.close()
    asyncio.run(run())


def test_failed_final_asr_preserves_preview_without_publishing_it_as_final(root):
    class FailedFinal(FakeWorker):
        async def send(self, value):
            if self.mode == "final":
                await self.events.put({"type": "failed", "error": "模拟最终识别失败", "fatal": False})
            else:
                await super().send(value)
    async def run():
        service = DictationService(root, worker_factory=FailedFinal)
        state = await service.create("owner", SessionRequest())
        await ready(state)
        runtime = service.runtimes[state["id"]]
        await runtime.accept_audio(struct.pack("<IQ", 0, 0) + b"\1\0" * 123)
        await runtime.stop()
        await asyncio.wait_for(runtime.task, 2)
        utterance = state["utterances"][0]
        assert utterance["status"] == "asr_failed" and utterance["preview"] == "预览"
        assert not utterance["raw_text"] and service.storage.render_draft(state) == ""
        assert (service.storage.path(state["id"]) / "audio/1.wav").is_file()
    asyncio.run(run())


@pytest.mark.parametrize("attached", [False, True])
def test_no_initial_connection_or_no_audio_is_bounded_and_gpu_reusable(root, attached):
    async def run():
        service = DictationService(root, worker_factory=FakeWorker)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        runtime = service.runtimes[state["id"]]
        runtime.attached = runtime.ever_attached = attached
        runtime.ready_at = time.monotonic() - 20
        runtime.last_audio_at = time.monotonic() - 20
        await asyncio.wait_for(asyncio.shield(runtime.task), 3)
        assert state["status"] in {"completed", "interrupted"}
        assert runtime.lock_file is None
        another = await service.create("owner", SessionRequest(cleanup=False), start=False)
        assert another["id"] != state["id"]
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["delete", "cancel", "close"])
def test_upload_reception_owned_until_exit_no_late_worker_start(root, operation):
    async def run():
        FakeWorker.started.clear()
        service = DictationService(root, worker_factory=FakeWorker)
        state = await service.create("owner", SessionRequest(), upload_name="upload.wav", start=False)
        sid = state["id"]
        entered = asyncio.Event()
        release = asyncio.Event()
        async def blocked_stream():
            yield b"partial upload"
            entered.set()
            await release.wait()
            yield b"should never be submitted"
        receiving = asyncio.create_task(service.receive_upload(sid, "owner", blocked_stream()))
        await entered.wait()
        lock = service.runtimes[sid].lock_file
        assert lock is not None
        if operation == "delete":
            await service.delete(sid, "owner")
        elif operation == "cancel":
            await service.cancel(sid, "owner")
        else:
            await service.close()
        release.set()
        assert receiving.cancelled()
        assert not FakeWorker.started
        assert lock.closed
        if operation == "delete":
            assert not service.storage.path(sid).exists()
        else:
            assert service.storage.load(sid)["status"] == ("interrupted" if operation == "close" else "cancelled")
        # Lifetime lock is gone only after the receiver has exited.
        with (root / "data/jobs/_asr-gpu.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(run())


def test_blocked_worker_pipe_bounds_audio_and_stop_and_preserves_received_pcm(root):
    from types import SimpleNamespace
    from src.dictation.service import Worker, WorkerIPCError
    class NeverDrain(FakeWorker):
        WRITE_TIMEOUT_SECONDS = .03
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.write_lock = asyncio.Lock()
            async def drain():
                await asyncio.Event().wait()
            self.process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=lambda value: None, drain=drain))
        async def send(self, command):
            if self.mode == "cpu":
                return await Worker.send(self, command)
            return await super().send(command)
    async def run():
        service = DictationService(root, worker_factory=NeverDrain)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        runtime = service.runtimes[state["id"]]
        pcm = b"\x01\x00" * 1280
        accepting = asyncio.create_task(runtime.accept_audio(struct.pack("<IQ", 0, 0) + pcm))
        await asyncio.sleep(.005)
        stopping = asyncio.create_task(runtime.stop())
        results = await asyncio.wait_for(asyncio.gather(accepting, stopping, return_exceptions=True), 1)
        assert all(isinstance(error, (WorkerIPCError, asyncio.CancelledError)) for error in results)
        await asyncio.wait_for(runtime.task, 1)
        assert state["status"] == "failed" and runtime.lock_file is None
        assert (service.storage.path(state["id"]) / "audio/source.pcm").read_bytes() == pcm
        with (root / "data/jobs/_asr-gpu.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(run())


def test_repeated_cancel_waits_the_same_worker_shutdown(root):
    class SlowClose(FakeWorker):
        async def close(self):
            await asyncio.sleep(.03)
            self.closed = True
    async def run():
        service = DictationService(root, worker_factory=SlowClose)
        state = await service.create("owner", SessionRequest())
        await ready(state)
        runtime = service.runtimes[state["id"]]
        first = asyncio.create_task(service.cancel(state["id"], "owner"))
        await asyncio.sleep(.005)
        second = asyncio.create_task(service.cancel(state["id"], "owner"))
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert runtime.cpu.closed and runtime.final.closed and runtime.lock_file is None
        assert state["status"] == "cancelled"
    asyncio.run(run())


def test_stop_written_but_cpu_never_drained_has_runtime_deadline(root):
    class NeverDrained(FakeWorker):
        async def send(self, command):
            if self.mode == "cpu" and command["op"] == "stop":
                return  # IPC succeeded, but the decoder never acknowledges completion.
            return await super().send(command)
    async def run():
        service = DictationService(root, worker_factory=NeverDrained)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        runtime = service.runtimes[state["id"]]
        runtime.CPU_DRAIN_TIMEOUT_SECONDS = .02
        pcm = b"\1\0" * 1000
        await runtime.accept_audio(struct.pack("<IQ", 0, 0) + pcm)
        await runtime.stop()
        await asyncio.wait_for(runtime.task, 2)
        assert state["status"] == "failed" and "收尾超过期限" in state["error"]
        assert runtime.lock_file is None and runtime.cpu.closed and runtime.final.closed
        assert (service.storage.path(state["id"]) / "audio/source.pcm").read_bytes() == pcm
        with (root / "data/jobs/_asr-gpu.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(run())


def test_startup_failure_cancels_other_loader_before_gpu_unlock(root):
    class BrokenWorker(FakeWorker):
        async def start(self):
            if self.mode == "cpu":
                raise RuntimeError("模拟流式模型加载失败")
            await asyncio.sleep(20)
    async def run():
        service = DictationService(root, worker_factory=BrokenWorker)
        state = await service.create("owner", SessionRequest())
        runtime = service.runtimes[state["id"]]
        await asyncio.wait_for(runtime.task, 1)
        assert state["status"] == "failed" and "加载失败" in state["error"]
        assert runtime.cpu.closed and runtime.final.closed and runtime.lock_file is None
    asyncio.run(run())


def test_http_snapshot_download_edit_and_owner_are_consistent(root):
    from api_server import create_app
    app = create_app(project_root=root)
    app.state.dictation.worker_factory = FakeWorker
    app.state.dictation.cleanup_factory = fake_cleanup
    headers = {"Origin": "http://testserver"}
    with TestClient(app) as client:
        config = client.get("/api/dictation/config").json()
        assert (config["cleanup_model"], config["cleanup_reasoning_effort"]) == ("gpt-6-luna", "low")
        assert (config["document_cleanup_model"], config["document_reasoning_effort"]) == ("gpt-6-luna", "high")
        assert client.post("/api/dictation/sessions", json={}).status_code == 403
        assert client.post("/api/dictation/sessions", json={"cleanup_model": "wrong"}, headers=headers).status_code == 422
        assert client.post("/api/dictation/sessions", json={"document_cleanup_model": "wrong"}, headers=headers).status_code == 422
        assert client.post("/api/dictation/sessions", json={"cleanup_reasoning_effort": "wrong"}, headers=headers).status_code == 422
        assert client.post("/api/dictation/sessions", json={"document_reasoning_effort": "wrong"}, headers=headers).status_code == 422
        response = client.post("/api/dictation/sessions", json={
            "cleanup_model": "gemini-3.5-flash-lite", "cleanup_reasoning_effort": "medium",
            "document_cleanup_model": "gemini-3.8-flash-high", "document_reasoning_effort": "high",
        }, headers=headers)
        assert response.status_code == 200
        assert response.json()["config"]["cleanup_model"] == "gemini-3.5-flash-lite"
        assert response.json()["config"]["reasoning_effort"] == "medium"
        assert response.json()["config"]["document_cleanup_model"] == "gemini-3.8-flash-high"
        assert response.json()["config"]["document_reasoning_effort"] == "high"
        sid = response.json()["id"]
        with client.websocket_connect(f"/api/dictation/sessions/{sid}/stream", headers=headers) as ws:
            for _ in range(100):
                snapshot = client.get(f"/api/dictation/sessions/{sid}").json()
                if snapshot["status"] == "listening":
                    break
                time.sleep(.005)
            ws.send_bytes(struct.pack("<IQ", 0, 0) + b"\1\0" * 500)
            ws.send_json({"type": "stop"})
            while True:
                event = ws.receive_json()
                if event.get("snapshot", {}).get("status") == "completed":
                    break
        endpoint = f"/api/dictation/sessions/{sid}"
        data = client.post(endpoint + "/cleanup", json={"scope": "document"}, headers=headers).json()
        data = client.post(endpoint + "/cleanup", json={"scope": "apply", "revision": data["revision"], "suggestion_id": data["document_suggestion"]["id"]}, headers=headers).json()
        assert client.get(endpoint + "/draft.txt").text == data["draft_text"]
        edited = client.patch(endpoint + "/draft", json={"revision": data["revision"], "text": "人工全文稿"}, headers=headers).json()
        assert client.get(endpoint).json()["draft_text"] == client.get(endpoint + "/draft.txt").text == "人工全文稿"
        original_cookie = client.cookies.get("dictation_owner")
        client.cookies.clear()
        client.cookies.set("dictation_owner", "other-owner")
        assert client.get(endpoint).status_code == 404
        client.cookies.set("dictation_owner", original_cookie)
        assert client.delete(endpoint, headers=headers).status_code == 200
        assert client.get(endpoint).status_code == 404

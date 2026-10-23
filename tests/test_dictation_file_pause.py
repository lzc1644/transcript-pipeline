from __future__ import annotations

import asyncio
import base64
import shutil
import threading
import wave

import pytest
from fastapi.testclient import TestClient

from src.dictation.service import DictationService
from src.dictation.schemas import SessionRequest
from src.dictation.storage import public_state
from tests.test_dictation import root, dripping_gateway
from tests.test_dictation_lifecycle import FakeWorker, ready
from tests.test_dictation_pause import status


class FileWorker(FakeWorker):
    asr_started = None
    asr_release = None
    decode_started = None
    decode_release = None
    asr_calls = []
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.uid = 0
    async def send(self, command):
        if self.mode == "final":
            self.asr_calls.append(command["utterance_id"])
            if self.asr_started:
                self.asr_started.set()
                await self.asr_release.wait()
            await super().send(command)
        elif command["op"] == "decode":
            if self.decode_started:
                self.decode_started.set()
                await self.decode_release.wait()
            shutil.copyfile(command["source"], command["destination"])
            with wave.open(command["source"]) as source:
                total = source.getnframes()
            await self.events.put({"type": "decoded", "samples": total})
        elif command["op"] == "audio":
            start = self.samples
            self.samples += len(base64.b64decode(command["pcm"])) // 2
            self.uid += 1
            await self.events.put({"type": "utterance.start", "utterance_id": str(self.uid), "start": start})
            await self.events.put({"type": "utterance.end", "utterance_id": str(self.uid), "start": start,
                "end": self.samples, "last_voice": self.samples, "boundary": "silence", "preview": ""})
            await self.events.put({"type": "processed", "samples": self.samples, "last_voice_sample": self.samples})
        elif command["op"] == "stop":
            await self.events.put({"type": "drained"})
        else:
            raise AssertionError("File pause must not send a CPU pause boundary")


async def upload(service, packets=3, cleanup=False):
    state = await service.create("owner", SessionRequest(cleanup=cleanup), upload_name="upload.wav", start=False)
    path = service.storage.path(state["id"]) / "upload.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\1\0" * (packets * 1280 - 3))
    r = service.runtimes[state["id"]]
    r.task = asyncio.create_task(r.run())
    await ready(state)
    return state, r


def test_pause_at_eof_waits_inflight_then_blocks_new_asr_and_preserves_queue(root):
    async def run():
        class Worker(FileWorker):
            asr_calls = []
            asr_started, asr_release = asyncio.Event(), asyncio.Event()
        service = DictationService(root, worker_factory=Worker)
        state, r = await upload(service)
        await Worker.asr_started.wait()
        await status(state, "draining")
        r.pause_file()
        assert state["status"] == "pausing" and r.lock_file is not None
        before = state["samples"]
        Worker.asr_release.set()
        await status(state, "paused")
        assert Worker.asr_calls == ["1"]
        assert public_state(state)["progress"]["transcribed_samples"] == 1280
        assert before == 3 * 1280 - 3
        await asyncio.sleep(.02)
        assert Worker.asr_calls == ["1"] and not r.task.done()
        r.pause_file()  # idempotent
        r.resume_file(); r.resume_file()
        await asyncio.wait_for(r.task, 2)
        assert state["status"] == "completed" and Worker.asr_calls == ["1", "2", "3"]
        assert state["samples"] == before and state["upload_cursor_samples"] == before
        assert [u["id"] for u in state["utterances"]] == ["1", "2", "3"]
        assert all(u["boundary"] == "silence" for u in state["utterances"])
        assert public_state(state)["progress"]["transcribed_samples"] == before
        assert not any("final_latency_seconds" in u for u in state["utterances"])
        assert r.lock_file is None
        r.pause_file()  # completed does not become an empty paused task
        assert state["status"] == "completed"
    asyncio.run(run())


def test_decode_already_started_finishes_before_pause_ack_without_dispatching_audio(root):
    async def run():
        class Worker(FileWorker):
            decode_started, decode_release = asyncio.Event(), asyncio.Event()
            asr_calls = []
        service = DictationService(root, worker_factory=Worker)
        state, r = await upload(service)
        await Worker.decode_started.wait()
        r.pause_file()
        assert state["status"] == "pausing"
        Worker.decode_release.set()
        await status(state, "paused")
        assert state["samples"] == 0 and not Worker.asr_calls
        r.resume_file()
        await asyncio.wait_for(r.task, 2)
        assert state["status"] == "completed" and state["samples"] == 3837
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["resume", "cancel", "stop"])
def test_full_queues_and_cleanup_pause_have_no_deadlock_or_new_requests(root, operation):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def factory(*args):
        def clean(text, *args):
            calls.append(text); entered.set(); release.wait(3)
            return {"text": text + "。", "needs_review": False}
        return clean
    async def run():
        class Worker(FileWorker):
            asr_calls = []
        service = DictationService(root, worker_factory=Worker, cleanup_factory=factory)
        state, r = await upload(service, packets=60, cleanup=True)
        assert await asyncio.to_thread(entered.wait, 2)
        async with asyncio.timeout(2):
            while not (r.queue.full() and r.cleanup_queue.full()):
                await asyncio.sleep(.001)
        r.pause_file()
        assert state["status"] == "pausing"
        release.set()
        await status(state, "paused")
        asr_before, cleanup_before, cursor = len(Worker.asr_calls), len(calls), state["samples"]
        await asyncio.sleep(.03)
        assert len(Worker.asr_calls) == asr_before and len(calls) == cleanup_before
        assert state["samples"] == cursor and r.lock_file is not None
        if operation == "resume":
            r.resume_file()
            await asyncio.wait_for(r.task, 3)
            assert state["status"] == "completed" and len(Worker.asr_calls) == 60 and len(calls) == 60
            assert len(set(Worker.asr_calls)) == 60
            assert (service.storage.path(state["id"]) / "audio/source.pcm").read_bytes() == b"\1\0" * (60 * 1280 - 3)
        elif operation == "cancel":
            await asyncio.wait_for(service.cancel(state["id"], "owner"), 2)
            assert state["status"] == "cancelled"
        else:
            await r.stop()
            await asyncio.wait_for(r.task, 3)
            assert state["status"] == "completed" and state["samples"] == cursor
            assert public_state(state)["progress"]["transcribed_samples"] == cursor
            assert cursor < state["audio_total_samples"]  # Early stop must not invent 100% completion.
        assert r.lock_file is None
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_waiting_cleanup_slot_cannot_start_a_request_after_pause(root):
    async def run():
        class Worker(FileWorker):
            asr_calls = []
        calls = []
        def factory(*args):
            def clean(text, *args):
                calls.append(text)
                return {"text": text, "needs_review": False}
            return clean
        service = DictationService(root, worker_factory=Worker, cleanup_factory=factory)
        await service.cleanup_slots.acquire(); await service.cleanup_slots.acquire()
        state, r = await upload(service, packets=3, cleanup=True)
        async with asyncio.timeout(2):
            while len(Worker.asr_calls) < 3:
                await asyncio.sleep(.001)
        r.pause_file()
        await status(state, "paused")
        service.cleanup_slots.release(); service.cleanup_slots.release()
        await asyncio.sleep(.02)
        assert not calls
        entered, release = threading.Event(), threading.Event()
        manual_calls = []
        def manual_cleanup():
            manual_calls.append(True)
            if len(manual_calls) == 2:
                entered.set()
            release.wait(2)
        manual = [asyncio.create_task(service.external(manual_cleanup)) for _ in range(2)]
        try:
            assert await asyncio.to_thread(entered.wait, .5), "paused file held a global cleanup slot"
            assert not calls and state["status"] == "paused"
        finally:
            release.set()
            await asyncio.gather(*manual)
        r.resume_file()
        await asyncio.wait_for(r.task, 2)
        assert len(calls) == 3 and state["status"] == "completed"
    asyncio.run(run())


def test_real_bounded_http_request_finishes_before_pause_without_new_dispatch(root, dripping_gateway, monkeypatch):
    from src.dictation.cleanup import Cleanup
    from src.schemas import CodexLBSettings
    port, started, disconnected, stats = dripping_gateway
    monkeypatch.setenv("DICTATION_FILE_PAUSE_KEY", "local-test-only")
    gateway = CodexLBSettings(base_url=f"http://127.0.0.1:{port}", base_url_env="", api_key_env="DICTATION_FILE_PAUSE_KEY")
    def factory(*args):
        return Cleanup(gateway, timeout=.25)
    async def run():
        class Worker(FileWorker):
            asr_calls = []
        service = DictationService(root, worker_factory=Worker, cleanup_factory=factory)
        state, r = await upload(service, packets=3, cleanup=True)
        assert await asyncio.to_thread(started.wait, 2)
        r.pause_file()
        assert state["status"] == "pausing"
        await status(state, "paused")
        assert await asyncio.to_thread(disconnected.wait, 1)
        await asyncio.sleep(.05)
        assert stats["requests"] == 1 and r.lock_file is not None
        r.resume_file()
        await asyncio.wait_for(r.task, 3)
        assert state["status"] == "completed" and stats["requests"] == 3
        assert public_state(state)["progress"]["cleanup_failed"] == 3
        assert all(u.get("raw_text") for u in state["utterances"])
    asyncio.run(run())


def test_new_history_cookie_survives_ambiguous_first_upload_and_owner_checks(root):
    from api_server import create_app
    app = create_app(project_root=root)
    app.state.dictation.worker_factory = FileWorker
    headers = {"Origin": "http://testserver"}
    with TestClient(app) as client:
        assert client.get("/api/dictation/sessions").json() == []
        cookie = client.cookies.get("dictation_owner")
        assert cookie
        response = client.post("/api/dictation/sessions", json={"cleanup": False}, headers=headers)
        sid = response.json()["id"]
        assert client.cookies.get("dictation_owner") == cookie
        assert client.post(f"/api/dictation/sessions/{sid}/pause", headers=headers).status_code == 409
        assert client.post(f"/api/dictation/sessions/{sid}/resume", headers=headers).status_code == 409
        assert client.post(f"/api/dictation/sessions/{sid}/pause").status_code == 403
        client.cookies.set("dictation_owner", "not-owner")
        assert client.post(f"/api/dictation/sessions/{sid}/pause", headers=headers).status_code == 404

"""Shared-worker contracts through service/API and the real line-framed process seam."""
from __future__ import annotations

import asyncio
import fcntl
import io
import json
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.dictation.schemas import SessionRequest
from src.dictation.service import Conflict, DictationService, Worker
from tests.test_dictation import root
from tests.test_dictation_file_pause import FileWorker, upload
from tests.test_dictation_lifecycle import ready
from tests.test_dictation_pause import status


class ControlledWorker(FileWorker):
    instances = []
    auto = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands = asyncio.Queue()
        self.instances.append(self)

    async def start(self):
        return {"test_worker": self.mode}

    async def send(self, command):
        if self.mode == "final":
            await self.commands.put(command)
            if self.auto:
                self.reply(command)
        elif command["op"] == "pause":
            await self.events.put({"type": "paused", "samples": self.samples})
        else:
            await super().send(command)

    def reply(self, command, **result):
        self.events.put_nowait({**{k: command[k] for k in ("session_id", "utterance_id", "request_id")},
            "type": "final", "text": " / ".join(command["terms"]) or command["session_id"],
            "inference_seconds": .001, **result})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


async def audio(runtime, count=1):
    for _ in range(count):
        await runtime.accept_audio(struct.pack("<IQ", runtime.state["next_sequence"], runtime.state["samples"]) + b"\1\0" * 1280)


async def live(service, owner="owner", terms=()):
    state = await service.create(owner, SessionRequest(cleanup=False, terms=list(terms)))
    await ready(state)
    assert state["status"] == "listening", state
    runtime = service.runtimes[state["id"]]
    runtime.attached = runtime.ever_attached = True
    return state, runtime


def gpu_available(root):
    with (root / "data/jobs/_asr-gpu.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_atomic_capacity_and_browser_credentials(root):
    async def run():
        service = DictationService(root, worker_factory=ControlledWorker)
        results = await asyncio.gather(*(service.create(str(i), SessionRequest(), start=False) for i in range(3)), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 2
        assert len(service.recognition.members) == 2
        assert "容量已满" in str(next(r for r in results if isinstance(r, Exception)))
        await service.close()
        gpu_available(root)
    asyncio.run(run())

    from api_server import create_app
    app = create_app(project_root=root)
    app.state.dictation.worker_factory = ControlledWorker
    headers = {"Origin": "http://testserver"}
    with TestClient(app) as client:
        ids = []
        for credential in ("browser-a", "browser-b", "browser-c"):
            client.cookies.clear(); client.cookies.set("dictation_owner", credential)
            response = client.post("/api/dictation/sessions", json={"cleanup": False}, headers=headers)
            if credential == "browser-c":
                assert response.status_code == 409 and "容量已满" in response.json()["detail"]
            else:
                assert response.status_code == 200
                ids.append(response.json()["id"])
        assert client.get("/api/dictation/config").json()["active_sessions"] == 2
        assert client.get(f"/api/dictation/sessions/{ids[0]}").status_code == 404
        client.cookies.clear(); client.cookies.set("dictation_owner", "browser-a")
        assert client.get(f"/api/dictation/sessions/{ids[1]}").status_code == 404
        client.post(f"/api/dictation/sessions/{ids[0]}/cancel", headers=headers)
        client.cookies.clear(); client.cookies.set("dictation_owner", "browser-c")
        assert client.post("/api/dictation/sessions", json={"cleanup": False}, headers=headers).status_code == 200


def test_one_worker_routes_identical_sentence_ids_and_terms_independently(root):
    async def run():
        class Workers(ControlledWorker):
            instances = []
        service = DictationService(root, worker_factory=Workers)
        a, ra = await live(service, "a", ["Alice", "1500"])
        b, rb = await live(service, "b", ["Bob", "HTTP API"])
        await asyncio.gather(audio(ra), audio(rb))
        await until(lambda: all(s["utterances"] and s["utterances"][0]["raw_text"] for s in (a, b)))
        assert [s["utterances"][0]["id"] for s in (a, b)] == ["1", "1"]
        assert a["utterances"][0]["text"] == "Alice / 1500"
        assert b["utterances"][0]["text"] == "Bob / HTTP API"
        assert [w.mode for w in Workers.instances].count("final") == 1
        assert [w.mode for w in Workers.instances].count("cpu") == 2
        for s in (a, b):
            result = json.loads((service.storage.path(s["id"]) / "asr/1.json").read_text())
            assert result["session_id"] == s["id"] and result["utterance_id"] == "1"
            assert result["request_id"] and result["queue_wait_seconds"] >= 0
        await asyncio.gather(ra.stop(), rb.stop())
        await asyncio.gather(ra.task, rb.task)
        assert a["status"] == b["status"] == "completed"
        assert all(w.closed for w in Workers.instances)
        gpu_available(root)
    asyncio.run(run())


def test_file_and_live_round_robin_and_queue_wait_is_not_inference_timeout(root):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        service.recognition.INFERENCE_TIMEOUT_SECONDS = .15
        a, ra = await upload(service, packets=6)
        worker = service.recognition.worker
        first = await asyncio.wait_for(worker.commands.get(), 1)
        b, rb = await live(service, "b", ["live"])
        await audio(rb, 3)
        await until(lambda: any(r.runtime is rb for r in service.recognition.pending))
        order = [first["session_id"]]
        for _ in range(8):
            await asyncio.sleep(.035)  # B's third turn waits longer than the per-inference deadline.
            worker.reply(first)
            first = await asyncio.wait_for(worker.commands.get(), 1)
            order.append(first["session_id"])
        worker.reply(first)
        assert order[:6] == [a["id"], b["id"]] * 3
        assert order[6:] == [a["id"]] * 3
        await rb.stop()
        await asyncio.wait_for(asyncio.gather(ra.task, rb.task), 2)
        assert a["status"] == b["status"] == "completed"
        assert b["utterances"][2]["queue_wait_seconds"] > .15
        assert all(u["asr_seconds"] == .001 for u in b["utterances"])
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["pause", "stop", "cancel", "delete", "disconnect"])
def test_one_lifecycle_does_not_stop_other_session(root, operation):
    async def run():
        service = DictationService(root, worker_factory=ControlledWorker)
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        worker = service.recognition.worker
        await audio(ra)
        await until(lambda: bool(a["utterances"]) and bool(a["utterances"][0]["raw_text"]))
        if operation == "pause":
            await ra.pause(a["next_sequence"], a["samples"])
            await status(a, "paused")
            assert ra.asr_reserved
        elif operation == "stop":
            await ra.stop(); await ra.task
        elif operation in {"cancel", "delete"}:
            await getattr(service, operation)(a["id"], "a")
        else:
            ra.disconnected_at = 1
            await asyncio.wait_for(ra.task, 2)
            assert a["status"] == "interrupted"
        await audio(rb)
        await until(lambda: bool(b["utterances"]) and bool(b["utterances"][0]["raw_text"]))
        assert b["status"] == "listening" and not worker.closed
        if operation == "pause":
            await ra.resume(a["next_sequence"], a["samples"])
            await audio(ra)
            await until(lambda: len(a["utterances"]) == 2 and bool(a["utterances"][1]["raw_text"]))
        await service.close()
        gpu_available(root)
    asyncio.run(run())


def test_paused_file_skips_queued_request_and_does_not_hold_gpu_turn(root):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        b, rb = await live(service, "b")
        await audio(rb)
        worker = service.recognition.worker
        command = await worker.commands.get()
        a, ra = await upload(service, packets=3)
        await until(lambda: any(r.runtime is ra for r in service.recognition.pending))
        ra.pause_file()
        await status(a, "paused")  # queued, not in flight
        worker.reply(command)
        await audio(rb)
        second = await asyncio.wait_for(worker.commands.get(), 1)
        assert second["session_id"] == b["id"]
        worker.reply(second)
        assert not a.get("asr_processed_samples") and ra.asr_reserved
        ra.resume_file()
        for _ in range(3):
            command = await asyncio.wait_for(worker.commands.get(), 1)
            assert command["session_id"] == a["id"]
            worker.reply(command)
        await rb.stop()
        await asyncio.wait_for(asyncio.gather(ra.task, rb.task), 2)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["cancel", "delete"])
def test_cancel_removes_pending_waits_own_inflight_and_no_late_files(root, operation):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        worker = service.recognition.worker
        await audio(ra, 3)
        active = await worker.commands.get()
        await audio(rb)
        await until(lambda: any(r.runtime is rb for r in service.recognition.pending))
        exiting = asyncio.create_task(getattr(service, operation)(a["id"], "a"))
        await asyncio.sleep(.01)
        assert not exiting.done() and not worker.closed
        worker.reply(active)
        await asyncio.wait_for(exiting, 1)
        other = await asyncio.wait_for(worker.commands.get(), 1)
        assert other["session_id"] == b["id"]
        worker.reply(other)
        await rb.stop(); await rb.task
        assert not (service.storage.path(a["id"]) / "asr/1.json").exists()
        if operation == "delete":
            assert not service.storage.path(a["id"]).exists()
        gpu_available(root)
    asyncio.run(run())


def test_cancel_queued_session_does_not_wait_for_other_inflight(root):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        await audio(ra)
        worker = service.recognition.worker
        command = await worker.commands.get()
        await audio(rb)
        await until(lambda: any(r.runtime is rb for r in service.recognition.pending))
        await asyncio.wait_for(service.delete(b["id"], "b"), .2)
        assert not service.recognition.pending and not worker.closed
        worker.reply(command)
        await ra.stop(); await ra.task
        assert worker.commands.empty()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["live", "upload"])
def test_only_remote_cleanup_releases_recognition_capacity(root, mode):
    entered, release = threading.Event(), threading.Event()
    def factory(*args):
        def clean(text, *args):
            entered.set(); assert release.wait(3)
            return {"text": text, "needs_review": False}
        return clean
    async def run():
        cfg = root / "config/dictation.json"
        cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "max_active_sessions": 1}))
        service = DictationService(root, worker_factory=ControlledWorker, cleanup_factory=factory)
        try:
            if mode == "upload":
                a, ra = await upload(service, packets=1, cleanup=True)
            else:
                a = await service.create("a", SessionRequest(cleanup=True))
                await ready(a); ra = service.runtimes[a["id"]]
                await audio(ra); await ra.stop()
            assert await asyncio.to_thread(entered.wait, 1)
            await until(lambda: not ra.asr_reserved)
            assert not ra.task.done()
            b, rb = await live(service, "b")
            assert b["status"] == "listening"
        finally:
            release.set(); await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["crash", "timeout", "oom", "identity", "idle_crash"])
def test_shared_failure_settles_every_waiter_and_allows_fresh_start(root, failure):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        service.recognition.INFERENCE_TIMEOUT_SECONDS = .04
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        worker = service.recognition.worker
        if failure != "idle_crash":
            await audio(ra, 2)
            command = await worker.commands.get()
            await audio(rb)
            await until(lambda: any(r.runtime is rb for r in service.recognition.pending))
            if failure == "oom":
                worker.reply(command, type="failed", error="CUDA out of memory", fatal=True)
            elif failure == "identity":
                worker.reply(command, request_id="stale")
            elif failure == "crash":
                worker.events.put_nowait({"type": "error", "error": "worker crashed"})
        else:
            worker.events.put_nowait({"type": "error", "error": "idle worker crashed"})
        await asyncio.wait_for(asyncio.gather(ra.task, rb.task), 1)
        assert a["status"] == b["status"] == "failed"
        assert not service.recognition.members and worker.closed
        assert not service.recognition.pending and service.recognition.active is None
        assert all(u["status"] == "asr_failed" for s in (a, b) for u in s["utterances"])
        assert (service.storage.path(a["id"]) / "audio/source.pcm").is_file()
        gpu_available(root)
        c, rc = await live(service, "c")
        assert service.recognition.worker is not worker
        await service.close()
    asyncio.run(run())


def test_single_sentence_error_does_not_fail_other_session_or_worker(root):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        await audio(ra); await audio(rb)
        worker = service.recognition.worker
        first = await worker.commands.get()
        worker.reply(first, type="failed", error="bad audio", fatal=False)
        second = await worker.commands.get(); worker.reply(second)
        await ra.stop(); await rb.stop()
        await asyncio.gather(ra.task, rb.task)
        assert a["status"] == b["status"] == "completed"
        assert sorted(s["utterances"][0]["status"] for s in (a, b)) == ["asr_failed", "raw"]
    asyncio.run(run())


def test_upload_waits_without_gpu_or_capacity_and_can_resume_without_reupload(root):
    async def run():
        service = DictationService(root, worker_factory=ControlledWorker)
        lock_path = root / "data/jobs/_asr-gpu.lock"
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("a") as batch:
            fcntl.flock(batch, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = await service.create("owner", SessionRequest(cleanup=False), upload_name="upload.wav", start=False)
            assert not service.recognition.members and service.recognition.lock_file is None
            data = io.BytesIO()
            import wave
            with wave.open(data, "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed")); wav.writeframes(b"\1\0" * 997)
            async def chunks():
                yield data.getvalue()
            await service.receive_upload(state["id"], "owner", chunks())
            assert state["status"] == "waiting" and state["wait_reason"] == "batch_asr"
            with pytest.raises(Conflict, match="GPU"):
                await service.start_upload(state["id"], "owner")
        path = service.storage.path(state["id"]) / "upload.wav"
        assert path.read_bytes() == data.getvalue()
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        with pytest.raises(Conflict, match="容量"):
            await service.start_upload(state["id"], "owner")
        assert state["wait_reason"] == "capacity"
        await service.cancel(a["id"], "a")
        await service.start_upload(state["id"], "owner")
        await asyncio.wait_for(service.runtimes[state["id"]].task, 2)
        assert state["status"] == "completed" and path.read_bytes() == data.getvalue()
        await service.close()
    asyncio.run(run())


def test_final_worker_protocol_uses_task_terms_and_echoes_identity(monkeypatch):
    import src.dictation.worker as worker
    import src.asr.utterance as final_asr
    calls = []
    def transcribe(path, terms):
        calls.append((str(path), terms))
        if "bad" in terms:
            raise ValueError("bad audio")
        return terms[0]
    torch = SimpleNamespace(set_num_threads=lambda _: None, __version__="test", version=SimpleNamespace(cuda="test"),
        cuda=SimpleNamespace(reset_peak_memory_stats=lambda: None, max_memory_allocated=lambda: 1, memory_allocated=lambda: 1))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(final_asr, "UtteranceASR", lambda _: SimpleNamespace(transcribe=transcribe,
        backend=SimpleNamespace(model=SimpleNamespace(forced_aligner=None))))
    commands = [{"op": "transcribe", "session_id": sid, "utterance_id": "1", "request_id": sid + "-request",
                 "path": sid + ".wav", "terms": terms} for sid, terms in [("a", ["Alice"]), ("b", ["Bob"]), ("c", ["bad"])]]
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO("\n".join(json.dumps(c) for c in commands)))
    results = []
    monkeypatch.setattr(worker, "emit", results.append)
    worker.final_worker({"qwen_cache": "unused"})
    assert calls == [("a.wav", ["Alice"]), ("b.wav", ["Bob"]), ("c.wav", ["bad"])]
    assert [r["type"] for r in results] == ["ready", "final", "final", "failed"]
    for command, result in zip(commands, results[1:]):
        assert all(command[k] == result[k] for k in ("session_id", "utterance_id", "request_id"))


@pytest.mark.parametrize("outcome", ["success", "crash", "hang"])
def test_actual_ipc_process_exits_before_gpu_mutex_is_released(root, outcome):
    script = root / "ipc_probe.py"
    script.write_text('''import json, os, sys, time
print(json.dumps({"type":"ready"}), flush=True)
for line in sys.stdin:
    c=json.loads(line)
    if "crash" in c["terms"]: os._exit(7)
    if "hang" in c["terms"]: time.sleep(20)
    print(json.dumps({**{k:c[k] for k in ("session_id","utterance_id","request_id")},
          "type":"final", "text":c["session_id"], "inference_seconds":.001}), flush=True)
# Acknowledging EOF alone is not process exit.
time.sleep(.03)
''')
    instances = []
    class ProcessWorker(Worker):
        async def start(self):
            self.process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(script),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                pass_fds=(self.lock_fd,))
            instances.append(self)
            assert (await self.read())["type"] == "ready"
            return {"ipc_probe": True}

        async def close(self):
            await super().close()
            assert self.process.returncode is not None
            with (root / "data/jobs/_asr-gpu.lock").open("a") as other:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def factory(*args):
        return (ProcessWorker if args[1] == "final" else ControlledWorker)(*args)

    async def run():
        service = DictationService(root, worker_factory=factory)
        service.recognition.INFERENCE_TIMEOUT_SECONDS = .05
        a, ra = await live(service, "a", [outcome])
        b, rb = await live(service, "b")
        await audio(ra); await audio(rb)
        if outcome == "success":
            await ra.stop(); await rb.stop()
        await asyncio.wait_for(asyncio.gather(ra.task, rb.task), 5)
        assert a["status"] == b["status"] == ("completed" if outcome == "success" else "failed")
        assert len(instances) == 1 and instances[0].process.returncode is not None
        assert service.recognition.lock_file is None
        gpu_available(root)
    asyncio.run(run())


def test_shared_loading_survives_one_cpu_start_failure(root):
    async def run():
        class Workers(ControlledWorker):
            final_started = asyncio.Event()
            allow_load = asyncio.Event()
            cpu_count = 0
            async def start(self):
                if self.mode == "final":
                    self.final_started.set()
                    await self.allow_load.wait()
                else:
                    Workers.cpu_count += 1
                    if Workers.cpu_count == 1:
                        raise RuntimeError("CPU load failed")
                return {}
        service = DictationService(root, worker_factory=Workers)
        a = await service.create("a", SessionRequest(cleanup=False))
        b = await service.create("b", SessionRequest(cleanup=False))
        await Workers.final_started.wait()
        await asyncio.wait_for(service.runtimes[a["id"]].task, 1)
        assert a["status"] == "failed" and len(service.recognition.members) == 1
        Workers.allow_load.set()
        await ready(b)
        assert b["status"] == "listening"
        await service.close()
        gpu_available(root)
    asyncio.run(run())


def test_service_shutdown_ends_shared_inflight_and_preserves_received_audio(root):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        a, ra = await live(service, "a")
        b, rb = await live(service, "b")
        await audio(ra); await audio(rb)
        worker = service.recognition.worker
        await worker.commands.get()
        await asyncio.wait_for(service.close(), 1)
        assert worker.closed and service.recognition.lock_file is None
        for s in (a, b):
            assert s["status"] == "interrupted"
            assert len((service.storage.path(s["id"]) / "audio/source.pcm").read_bytes()) == 2560
        gpu_available(root)
    asyncio.run(run())


def test_realtime_full_queue_stops_capture_and_drains_every_received_sentence(root):
    async def run():
        class Workers(ControlledWorker):
            auto = False
        service = DictationService(root, worker_factory=Workers)
        a, ra = await live(service)
        await audio(ra, 24)
        await status(a, "draining")
        assert "识别队列已满" in a["phase"] and ra.queue.qsize() <= 16
        worker = service.recognition.worker
        command = await worker.commands.get()
        worker.auto = True
        worker.reply(command)
        await asyncio.wait_for(ra.task, 2)
        assert a["status"] == "completed" and len(a["utterances"]) == 24
        assert all(u["status"] == "raw" for u in a["utterances"])
        assert (service.storage.path(a["id"]) / "audio/source.pcm").read_bytes() == b"\1\0" * (24 * 1280)
        assert any(e["type"] == "session.warning" and "识别队列已满" in e["message"] for e in ra.events)
    asyncio.run(run())


def test_waiting_upload_resume_api_checks_owner_and_reuses_saved_material(root):
    from api_server import create_app
    app = create_app(project_root=root)
    app.state.dictation.worker_factory = ControlledWorker
    headers = {"Origin": "http://testserver"}
    data = io.BytesIO()
    import wave
    with wave.open(data, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed")); wav.writeframes(b"\1\0" * 997)
    lock_path = root / "data/jobs/_asr-gpu.lock"
    lock_path.parent.mkdir(parents=True)
    with TestClient(app) as client:
        client.get("/api/dictation/sessions")
        cookie = client.cookies.get("dictation_owner")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            response = client.post("/api/dictation/uploads?filename=voice.wav&cleanup=false", content=data.getvalue(), headers=headers)
            assert response.status_code == 200
            sid = response.json()["id"]
            assert response.json()["status"] == "waiting"
            assert client.post(f"/api/dictation/sessions/{sid}/resume", headers=headers).status_code == 409
        client.cookies.clear(); client.cookies.set("dictation_owner", "other-browser")
        assert client.post(f"/api/dictation/sessions/{sid}/resume", headers=headers).status_code == 404
        client.cookies.clear(); client.cookies.set("dictation_owner", cookie)
        assert client.post(f"/api/dictation/sessions/{sid}/resume", headers=headers).status_code == 200
        assert (app.state.dictation.storage.path(sid) / "upload.wav").read_bytes() == data.getvalue()


def test_cancel_wins_when_upload_admission_is_waiting_for_worker_reclamation(root):
    async def run():
        service = DictationService(root, worker_factory=ControlledWorker)
        state = await service.create("a", SessionRequest(cleanup=False), upload_name="upload.wav", start=False)
        state.update(upload_bytes=3, status="waiting")
        runtime = service.runtimes[state["id"]]
        await service.recognition.lifecycle.acquire()
        starting = asyncio.create_task(service.start_upload(state["id"], "a"))
        await until(lambda: service.gate.locked())
        cancelling = asyncio.create_task(service.cancel(state["id"], "a"))
        await until(lambda: runtime.cancelled)
        service.recognition.lifecycle.release()
        with pytest.raises(Conflict, match="取消"):
            await starting
        await cancelling
        assert runtime.task is None and not runtime.asr_reserved and not service.recognition.members
        assert state["status"] == "cancelled"
    asyncio.run(run())

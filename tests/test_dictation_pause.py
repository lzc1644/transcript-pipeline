"""Pause is a durable audio boundary, not CPU EOF or a second session."""
from __future__ import annotations

import asyncio
import base64
import fcntl
import io
import json
import struct
import time
import wave

import pytest
from fastapi.testclient import TestClient

from src.dictation.audio import RATE, Segmenter
from src.dictation.schemas import SessionRequest
from src.dictation.service import Conflict, DictationService
from tests.test_dictation import root, fake_cleanup
from tests.test_dictation_lifecycle import FakeWorker, ready


class PauseWorker(FakeWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.segmenter = Segmenter(lambda frame: 1)
        self.uid = 0

    async def send(self, command):
        if self.mode == "final":
            return await super().send(command)
        op = command["op"]
        events = (self.segmenter.pause() if op == "pause" else
                  self.segmenter.feed(base64.b64decode(command.get("pcm", "")), finish=op == "stop"))
        for event in events:
            if event["type"] == "start":
                self.uid += 1
                await self.events.put({"type": "utterance.start", "utterance_id": str(self.uid), "start": event["start"]})
            elif event["type"] == "end":
                await self.events.put({**event, "type": "utterance.end", "utterance_id": str(self.uid), "preview": "预览"})
        await self.events.put({"type": "processed", "samples": self.segmenter.position,
                               "last_voice_sample": self.segmenter.last_activity})
        if op in {"pause", "stop"}:
            await self.events.put({"type": "paused" if op == "pause" else "drained", "samples": self.segmenter.position})


async def status(state, expected):
    async with asyncio.timeout(2):
        while state["status"] != expected:
            assert state["status"] != "failed", state
            await asyncio.sleep(.001)


@pytest.mark.parametrize("length", [1, 200, 512, 997, 1281])
def test_segmenter_pause_keeps_tail_resets_acoustic_context_not_position(length):
    class VAD:
        resets = 0
        def __call__(self, frame):
            return 1
        def reset(self):
            self.resets += 1
    vad = VAD()
    segmenter = Segmenter(vad)
    first = segmenter.feed(b"\1\0" * length) + segmenter.pause()
    assert b"".join(e["pcm"] for e in first if e["type"] == "audio") == b"\1\0" * length
    assert [e for e in first if e["type"] == "end"] == [{"type": "end", "start": 0, "end": length,
        "last_voice": length, "boundary": "pause"}]
    assert segmenter.pause() == []  # no phantom repeated tail
    second = segmenter.feed(b"\2\0" * 123, finish=True)
    assert [e["start"] for e in second if e["type"] == "start"] == [length]
    assert [e["end"] for e in second if e["type"] == "end"] == [length + 123]
    assert vad.resets == 2 and not segmenter.pending and not segmenter.pre


def test_cpu_protocol_pause_does_not_exit_or_reset_sentence_ids(monkeypatch):
    import importlib.metadata
    import src.dictation.worker as worker
    import src.asr.streaming_audio as audio
    import src.asr.streaming as streaming
    monkeypatch.setattr(audio, "SileroVAD", lambda path: lambda frame: 1)
    monkeypatch.setattr(streaming, "StreamingASR", lambda path: None)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "test-isolated-worker")
    commands = [{"op": "audio", "pcm": base64.b64encode(b"\1\0" * 997).decode()}, {"op": "pause"},
                {"op": "audio", "pcm": base64.b64encode(b"\2\0" * 211).decode()}, {"op": "pause"}, {"op": "stop"}]
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO("\n".join(json.dumps(c) for c in commands)))
    events = []
    monkeypatch.setattr(worker, "emit", events.append)
    worker.cpu_worker({"vad_model": "unused", "segmentation": {}, "preview": False})
    assert [e["samples"] for e in events if e["type"] == "paused"] == [997, 1208]
    ends = [e for e in events if e["type"] == "utterance.end"]
    assert [(e["utterance_id"], e["start"], e["end"], e["boundary"]) for e in ends] == [("1", 0, 997, "pause"), ("2", 997, 1208, "pause")]
    assert events[-1]["type"] == "drained"


def test_twenty_pause_resume_cycles_keep_pcm_ids_gpu_and_manual_edit(root):
    async def run():
        service = DictationService(root, worker_factory=PauseWorker, cleanup_factory=fake_cleanup)
        state = await service.create("owner", SessionRequest())
        await ready(state)
        r = service.runtimes[state["id"]]
        r.attached = r.ever_attached = True
        chunks = []
        for i in range(20):
            pcm = struct.pack("<h", i + 1) * (997 + i)
            packet = struct.pack("<IQ", state["next_sequence"], state["samples"]) + pcm
            chunks.append(pcm)
            await r.accept_audio(packet)
            boundary = state["next_sequence"], state["samples"]
            await r.pause(*boundary)
            await r.pause(*boundary)
            await status(state, "paused")
            await r.pause(*boundary)
            assert not r.task.done() and r.asr_reserved
            with (root / "data/jobs/_asr-gpu.lock").open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Replayed ACK packets remain idempotent while paused; new samples are refused.
            await r.accept_audio(packet)
            with pytest.raises(Conflict):
                await r.accept_audio(struct.pack("<IQ", *boundary) + b"\0\0")
            await r.resume(*boundary)
            await r.resume(*boundary)
        await r.stop()
        await asyncio.wait_for(r.task, 3)
        assert state["status"] == "completed" and len(state["utterances"]) == 20
        assert len(state["pause_boundaries"]) == 20
        assert [u["id"] for u in state["utterances"]] == [str(i + 1) for i in range(20)]
        assert all(u["boundary"] == "pause" for u in state["utterances"])
        assert all(a["end_sample"] == b["start_sample"] for a, b in zip(state["utterances"], state["utterances"][1:]))
        directory = service.storage.path(state["id"])
        assert (directory / "audio/source.pcm").read_bytes() == b"".join(chunks)
        for u, pcm in zip(state["utterances"], chunks):
            with wave.open(str(directory / f'audio/{u["id"]}.wav')) as wav:
                assert wav.readframes(wav.getnframes()) == pcm
        assert not r.asr_reserved
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["stop", "cancel", "delete"])
def test_terminal_actions_win_over_pending_pause_or_resume(root, operation):
    class SlowPause(PauseWorker):
        async def send(self, command):
            if self.mode == "cpu" and command["op"] == "pause":
                # Queueing pause succeeded; its acknowledgement has not arrived.
                return
            await super().send(command)
    async def run():
        service = DictationService(root, worker_factory=SlowPause)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        r = service.runtimes[state["id"]]
        await r.accept_audio(struct.pack("<IQ", 0, 0) + b"\1\0" * 211)
        await r.pause(1, 211)
        assert state["status"] == "pausing"
        if operation == "stop":
            await r.stop()
            await r.cpu.events.put({"type": "paused", "samples": 211})
            await asyncio.wait_for(r.task, 2)
            assert state["status"] == "completed"
        else:
            await getattr(service, operation)(state["id"], "owner")
            assert state["status"] == "cancelled"
        with pytest.raises(Conflict):
            await r.resume(1, 211)
        assert state["status"] != "listening" and not r.asr_reserved
    asyncio.run(run())


def test_paused_skips_idle_and_no_audio_but_disconnect_still_expires(root):
    async def run():
        service = DictationService(root, worker_factory=PauseWorker)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        r = service.runtimes[state["id"]]
        r.attached = r.ever_attached = True
        await r.pause(0, 0)
        await status(state, "paused")
        r.last_audio_at = r.ready_at = time.monotonic() - 100
        r.processed = RATE * 70  # would trigger the previous idle rule
        await asyncio.sleep(1.1)
        assert state["status"] == "paused" and not r.task.done()
        r.attached = False
        r.disconnected_at = time.monotonic() - 100
        await asyncio.wait_for(r.task, 2)
        assert state["status"] == "interrupted" and not r.asr_reserved
    asyncio.run(run())


def test_pause_confirmation_is_bounded_and_boundary_must_match(root):
    class NeverPaused(PauseWorker):
        async def send(self, command):
            if self.mode == "cpu" and command["op"] == "pause":
                return
            await super().send(command)
    async def run():
        service = DictationService(root, worker_factory=NeverPaused)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        r = service.runtimes[state["id"]]
        for boundary in [(1, 0), (0, 1), (True, 0), (None, None)]:
            with pytest.raises(Conflict, match="控制边界"):
                await r.pause(*boundary)
        assert state["status"] == "listening"
        r.CPU_DRAIN_TIMEOUT_SECONDS = .02
        await r.pause(0, 0)
        await asyncio.wait_for(r.task, 2)
        assert state["status"] == "failed" and "暂停确认" in state["error"] and not r.asr_reserved
    asyncio.run(run())


@pytest.mark.parametrize("old_status", ["pausing", "paused"])
def test_restart_marks_pause_interrupted_and_old_sessions_need_no_new_fields(root, old_status):
    async def run():
        service = DictationService(root)
        state = await service.create("owner", SessionRequest(), start=False)
        state["status"] = old_status
        service.commit(state)  # intentionally no pause_boundaries field
        await service.runtimes[state["id"]].release_recognition()
        recovered = DictationService(root)
        snapshot = recovered.get(state["id"], "owner")
        assert snapshot["status"] == "interrupted"
        assert "pause_boundaries" not in snapshot
    asyncio.run(run())


def test_sample_latency_uses_original_capture_span_and_resets_idle_on_resume(root):
    async def run():
        service = DictationService(root, worker_factory=PauseWorker)
        state = await service.create("owner", SessionRequest(cleanup=False))
        await ready(state)
        r = service.runtimes[state["id"]]
        await r.accept_audio(struct.pack("<IQ", 0, 0) + b"\1\0" * 997)
        await r.pause(1, 997)
        await status(state, "paused")
        before = time.monotonic()
        await r.resume(1, 997)
        assert r.last_audio_at >= before and r.idle_start_sample == 997 and r.silence_seconds() == 0
        r.audio_spans = [(0, 100), (997, 200)]
        assert r.sample_time(997) == 100 + 997 / RATE
        assert r.sample_time(997, endpoint=False) == 200
        assert r.sample_time(1097) == 200 + 100 / RATE
        await service.cancel(state["id"], "owner")
    asyncio.run(run())


def test_websocket_pause_and_resume_behind_audio_and_without_new_session(root):
    from api_server import create_app
    app = create_app(project_root=root)
    app.state.dictation.worker_factory = PauseWorker
    headers = {"Origin": "http://testserver"}
    with TestClient(app) as client:
        sid = client.post("/api/dictation/sessions", json={"cleanup": False}, headers=headers).json()["id"]
        with client.websocket_connect(f"/api/dictation/sessions/{sid}/stream", headers=headers) as ws:
            while True:
                event = ws.receive_json()
                if event.get("snapshot", {}).get("status") == "listening":
                    break
            ws.send_bytes(struct.pack("<IQ", 0, 0) + b"\1\0" * 211)
            ws.send_json({"type": "pause", "next_sequence": 1, "samples": 211})
            while True:
                event = ws.receive_json()
                if event["type"] == "session.paused":
                    break
            assert event["snapshot"]["samples"] == 211 and event["snapshot"]["id"] == sid
            ws.send_json({"type": "resume", "next_sequence": 1, "samples": 211})
            while ws.receive_json()["type"] != "session.resumed":
                pass
            ws.send_bytes(struct.pack("<IQ", 1, 211) + b"\2\0" * 123)
            ws.send_json({"type": "stop"})
            while True:
                event = ws.receive_json()
                if event.get("snapshot", {}).get("status") == "completed":
                    break
            assert event["snapshot"]["samples"] == 334
            assert [(u["start_sample"], u["end_sample"]) for u in event["snapshot"]["utterances"]] == [(0, 211), (211, 334)]
        assert len(client.get("/api/dictation/sessions").json()) == 1

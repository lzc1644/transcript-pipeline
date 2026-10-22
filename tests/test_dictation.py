from __future__ import annotations

import asyncio
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import struct
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from src.dictation.audio import RATE, Segmenter, SegmentConfig
from src.codex_lb_client import CodexLBClientError, should_use_curl_first
from src.dictation.cleanup import Cleanup, SessionGatewayClient, document_chunks, validate_cleanup
from src.schemas import CodexLBSettings
from src.dictation.schemas import CleanupRequest, SessionRequest
from src.dictation.service import Conflict, DictationService, Runtime
from src.dictation.storage import Storage, public_state


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "config/settings.yaml", tmp_path / "config/settings.yaml")
    for name in ("cpu", "final", "models/silero_vad_v6.onnx", "models/paraformer/encoder.int8.onnx"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "config/dictation.json").write_text(json.dumps({"cpu_python": "cpu", "final_python": "final", "model_dir": "models"}))
    return tmp_path


def fake_cleanup(*args, **kwargs):
    return lambda text, context, terms: {"text": text + "。", "needs_review": False, "metrics": {}}


async def completed(service):
    state = await service.create("owner", SessionRequest(), start=False)
    runtime = service.runtimes[state["id"]]
    runtime.release_gpu()
    state.update(status="completed", phase="完成")
    state["utterances"] = [{"id": "1", "revision": 1, "raw_text": "原稿", "text": "原稿", "edited": False,
                            "status": "cleanup_failed", "start_sample": 0, "end_sample": RATE}]
    service.commit(state)
    return state


def test_single_draft_projection_survives_save_retry_edit_and_reload(root):
    async def run():
        service = DictationService(root, cleanup_factory=fake_cleanup)
        state = await completed(service)
        sid = state["id"]
        await service.cleanup(sid, "owner", CleanupRequest(scope="document"))
        suggestion = state["document_suggestion"]
        await service.cleanup(sid, "owner", CleanupRequest(scope="apply", revision=state["revision"], suggestion_id=suggestion["id"]))
        expected = "原稿。"
        assert public_state(state)["draft_text"] == expected
        service.commit(state)  # status/ordinary save cannot revert an applied document
        await service.cleanup(sid, "owner", CleanupRequest(scope="failed"))
        assert Storage.render_draft(state) == expected
        assert (service.storage.path(sid) / "draft.txt").read_text() == expected
        recovered = DictationService(root, cleanup_factory=fake_cleanup)
        current = recovered.get(sid, "owner")
        assert public_state(current)["draft_text"] == expected
        with pytest.raises(Conflict, match="明确确认"):
            recovered.edit(sid, "owner", "1", current["utterances"][0]["revision"], "人工稿")
        result = recovered.edit_draft(sid, "owner", current["revision"], "人工全文")
        assert result["draft_text"] == "人工全文"
        result = recovered.edit(sid, "owner", "1", current["utterances"][0]["revision"], "人工逐句", True)
        assert result["draft_text"] == "人工逐句"
        assert (service.storage.path(sid) / "draft.txt").read_text() == result["draft_text"]
    asyncio.run(run())


@pytest.mark.parametrize("change", ["edit", "retry"])
def test_old_document_suggestion_cannot_apply_using_latest_session_revision(root, change):
    async def run():
        service = DictationService(root, cleanup_factory=fake_cleanup)
        state = await completed(service)
        sid = state["id"]
        await service.cleanup(sid, "owner", CleanupRequest(scope="document"))
        suggestion_id = state["document_suggestion"]["id"]
        if change == "edit":
            service.edit(sid, "owner", "1", state["utterances"][0]["revision"], "较新人工稿")
        else:
            await service.cleanup(sid, "owner", CleanupRequest(scope="failed"))
        assert "document_suggestion" not in state
        with pytest.raises(Conflict):
            await service.cleanup(sid, "owner", CleanupRequest(scope="apply", revision=state["revision"], suggestion_id=suggestion_id))
        assert "较新人工稿" in Storage.render_draft(state) if change == "edit" else Storage.render_draft(state) == "原稿。"
    asyncio.run(run())


def test_recovered_cleanup_does_not_overwrite_concurrent_edit(root):
    started, release = threading.Event(), threading.Event()
    def factory(*args):
        def call(*args):
            started.set()
            release.wait(2)
            return {"text": "迟到建议", "needs_review": False}
        return call
    async def run():
        original = DictationService(root)
        state = await completed(original)
        service = DictationService(root, cleanup_factory=factory)
        sid = state["id"]
        state = service.get(sid, "owner")
        revision = state["revision"]
        cleanup = asyncio.create_task(service.cleanup(sid, "owner", CleanupRequest(scope="failed")))
        while not started.is_set():
            await asyncio.sleep(.001)
        service.edit(sid, "owner", "1", 1, "我的编辑")
        release.set()
        await cleanup
        assert service.get(sid, "owner")["utterances"][0]["text"] == "我的编辑"
        assert state["utterances"][0]["suggestion"] == "迟到建议"
        assert state["revision"] >= revision + 2
        assert service.storage.load(sid)["utterances"][0]["text"] == "我的编辑"
    asyncio.run(run())


def test_document_cleanup_detects_concurrent_utterance_cleanup(root):
    started, release = threading.Event(), threading.Event()
    calls = 0
    def factory(*args):
        def call(text, *args):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                release.wait(2)
                return {"text": "旧全文建议", "needs_review": False}
            return {"text": "新逐句稿", "needs_review": False}
        return call
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        state = await completed(service)
        task = asyncio.create_task(service.cleanup(state["id"], "owner", CleanupRequest(scope="document")))
        while not started.is_set():
            await asyncio.sleep(.001)
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="failed"))
        release.set()
        with pytest.raises(Conflict, match="正文已修改"):
            await task
        assert "document_suggestion" not in state
        assert Storage.render_draft(state) == "新逐句稿"
    asyncio.run(run())


def test_delete_waits_for_external_cleanup_and_does_not_recreate_files(root):
    started, release = threading.Event(), threading.Event()
    def factory(*args):
        def call(*args):
            started.set()
            release.wait(2)
            return {"text": "迟到结果", "needs_review": False}
        return call
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        state = await completed(service)
        task = asyncio.create_task(service.cleanup(state["id"], "owner", CleanupRequest(scope="failed")))
        while not started.is_set():
            await asyncio.sleep(.001)
        deleting = asyncio.create_task(service.delete(state["id"], "owner"))
        await asyncio.sleep(.01)
        assert not deleting.done()
        with pytest.raises(Conflict, match="正在删除"):
            await service.cleanup(state["id"], "owner", CleanupRequest(scope="failed"))
        with pytest.raises(Conflict):
            service.edit(state["id"], "owner", "1", 1, "不能写")
        release.set()
        await deleting
        assert task.cancelled()
        assert not service.storage.path(state["id"]).exists()
    asyncio.run(run())


def test_close_cancels_and_waits_manual_cleanup(root):
    started, release = threading.Event(), threading.Event()
    def factory(*args):
        def call(*args):
            started.set()
            release.wait(2)
            return {"text": "迟到结果", "needs_review": False}
        return call
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        state = await completed(service)
        task = asyncio.create_task(service.cleanup(state["id"], "owner", CleanupRequest(scope="failed")))
        while not started.is_set():
            await asyncio.sleep(.001)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(.01)
        assert not closing.done()
        with pytest.raises(Conflict):
            await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        release.set()
        await closing
        assert task.cancelled()
        assert service.storage.load(state["id"])["utterances"][0]["text"] == "原稿"
    asyncio.run(run())


def test_close_cancels_runtime_before_waiting_for_slow_manual_cleanup(root):
    started, release = threading.Event(), threading.Event()
    runtime_cancelled = asyncio.Event()
    def factory(*args):
        def call(*args):
            started.set()
            release.wait(3)
            return {"text": "迟到结果", "needs_review": False}
        return call
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        state = await completed(service)
        async def runtime_work():
            try:
                await asyncio.Event().wait()
            finally:
                runtime_cancelled.set()
        runtime = service.runtimes[state["id"]]
        runtime.task = asyncio.create_task(runtime_work())
        manual = asyncio.create_task(service.cleanup(state["id"], "owner", CleanupRequest(scope="failed")))
        while not started.is_set():
            await asyncio.sleep(.001)
        closing = asyncio.create_task(service.close())
        try:
            await asyncio.wait_for(runtime_cancelled.wait(), .5)
            assert not closing.done()  # Remote thread still owns its slot.
            assert runtime.cancelled
        finally:
            release.set()
            await closing
        assert manual.cancelled() and runtime.task.done()
        assert service.storage.load(state["id"])["utterances"][0]["text"] == "原稿"
    asyncio.run(run())


def test_segmenter_keeps_extremely_short_speech_and_stop_tail():
    for length in (200, 512, 1027):
        segmenter = Segmenter(lambda frame: 1)
        raw = np.ones(length, dtype="<i2").tobytes()
        events = segmenter.feed(raw, finish=True)
        assert b"".join(e["pcm"] for e in events if e["type"] == "audio") == raw
        end = [e for e in events if e["type"] == "end"]
        assert end[-1]["end"] == length
        assert end[-1]["boundary"] == "stop"


def test_segmenter_silence_is_empty_and_long_speech_is_contiguous():
    assert Segmenter(lambda frame: 0).feed(bytes(RATE * 2), finish=True) == []
    segmenter = Segmenter(lambda frame: 1, SegmentConfig(hard_seconds=25))
    events = segmenter.feed(bytes(RATE * 2 * 56), finish=True)
    intervals = [(e["start"], e["end"]) for e in events if e["type"] == "end"]
    assert intervals[0][0] == 0 and intervals[-1][1] == RATE * 56
    assert all(a[1] == b[0] for a, b in zip(intervals, intervals[1:]))
    assert all(end - start <= 25 * RATE for start, end in intervals)


def test_idle_clock_uses_vad_not_partial_text(root):
    async def run():
        service = DictationService(root)
        state = await completed(service)
        runtime = service.runtimes[state["id"]]
        # A minute of voiced audio, no partial text at all.
        events = [{"type": "processed", "samples": RATE * 70, "last_voice_sample": RATE * 70}, {"type": "drained"}]
        class CPU:
            async def read(self):
                return events.pop(0)
        runtime.cpu = CPU()
        await runtime.cpu_loop()
        assert runtime.silence_seconds() == 0
        # A late text update during acoustic silence must not reset the clock.
        events[:] = [{"type": "processed", "samples": RATE * 140, "last_voice_sample": RATE * 70},
                     {"type": "partial", "utterance_id": "1", "text": "迟到文字"}, {"type": "drained"}]
        await runtime.cpu_loop()
        assert runtime.silence_seconds() == 70
    asyncio.run(run())


def test_audio_packet_idempotency_durability_and_gaps(root):
    async def run():
        service = DictationService(root)
        state = await service.create("owner", SessionRequest(), start=False)
        runtime = service.runtimes[state["id"]]
        runtime.cpu = SimpleNamespace(send=lambda *args: asyncio.sleep(0))
        state["status"] = "listening"
        packet = struct.pack("<IQ", 0, 0) + b"\x01\x00" * 1280
        await runtime.accept_audio(packet)
        await runtime.accept_audio(packet)
        assert state["samples"] == 1280 and state["next_sequence"] == 1
        assert service.storage.load(state["id"])["samples"] == 1280
        with pytest.raises(Conflict):
            await runtime.accept_audio(struct.pack("<IQ", 2, 1280) + b"\0\0")
        with pytest.raises(Conflict):
            await runtime.accept_audio(struct.pack("<IQ", 0, 0) + b"\0\0" * 1280)
        runtime.release_gpu()
    asyncio.run(run())


@pytest.mark.parametrize("value", ['{"text":"x","needs_review":0}', '{"text":"x"}', '{"text":"","needs_review":false}', 'partial', '{"text":"x","needs_review":false,"extra":1}'])
def test_cleanup_rejects_invalid_or_truncated_structure(value):
    with pytest.raises(ValueError):
        validate_cleanup(value)


def test_upload_limit_rejects_before_worker_and_releases_lock(root):
    async def run():
        from src.dictation.service import UploadLimit
        service = DictationService(root)
        state = await service.create("owner", SessionRequest(), upload_name="upload.wav", start=False)
        async def stream():
            yield b"not read"
        with pytest.raises(UploadLimit):
            await service.receive_upload(state["id"], "owner", stream(), 101 * 1024 * 1024)
        runtime = service.runtimes[state["id"]]
        assert runtime.task is None and runtime.lock_file is None
        assert state["status"] == "failed"
    asyncio.run(run())


def test_stalled_upload_has_receive_deadline_and_releases_gpu(root):
    async def run():
        service = DictationService(root)
        service.UPLOAD_RECEIVE_TIMEOUT_SECONDS = .02
        state = await service.create("owner", SessionRequest(), upload_name="upload.wav", start=False)
        async def stalled():
            yield b"partial"
            await asyncio.Event().wait()
        with pytest.raises(TimeoutError):
            await service.receive_upload(state["id"], "owner", stalled())
        assert service.runtimes[state["id"]].lock_file is None
        assert state["status"] == "failed" and "超过期限" in state["error"]
    asyncio.run(run())


def test_cleanup_failure_preserves_raw_and_manual_copy(root):
    def fail(*args):
        def call(*args):
            raise TimeoutError("模拟整理超时")
        return call
    async def run():
        service = DictationService(root, cleanup_factory=fail)
        state = await completed(service)
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="failed"))
        assert Storage.render_draft(state) == "原稿"
        assert state["utterances"][0]["raw_text"] == "原稿"
        assert state["utterances"][0]["status"] == "cleanup_failed"
    asyncio.run(run())


@pytest.fixture
def dripping_gateway():
    started, disconnected, stop = threading.Event(), threading.Event(), threading.Event()
    stats = {"chunks": 0}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.path == "/complete":
                response = {"type": "response.completed", "response": {"status": "completed", "model": "local-test",
                    "usage": {"input_tokens": 7, "output_tokens": 5}, "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": '{"text":"原稿。","needs_review":false}'}]}]}}
                self.wfile.write(("data: " + json.dumps(response) + "\n\n").encode())
                return
            started.set()
            try:
                # Never send response.completed; safety cap avoids hanging the
                # suite if a regression restores urllib's inactivity timeout.
                for _ in range(150):
                    if stop.wait(.02):
                        break
                    self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n')
                    self.wfile.flush()
                    stats["chunks"] += 1
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, started, disconnected, stats
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_cleanup_has_total_deadline_for_local_sse_that_never_completes(dripping_gateway, monkeypatch, host):
    port, started, disconnected, stats = dripping_gateway
    monkeypatch.setenv("DICTATION_DEADLINE_TEST_KEY", "local-test-only")
    gateway = CodexLBSettings(base_url=f"http://{host}:{port}", base_url_env="", api_key_env="DICTATION_DEADLINE_TEST_KEY")
    assert not should_use_curl_first(gateway.base_url)  # Batch policy stays unchanged.
    cleaner = Cleanup(gateway, timeout=.35)
    before = time.monotonic()
    with pytest.raises(CodexLBClientError, match="curl"):
        cleaner("原稿", [], [])
    assert time.monotonic() - before < 1.5
    assert started.is_set() and stats["chunks"] >= 3
    assert disconnected.wait(1)  # The actual connection, not just its awaiter, closed.


def test_close_waits_for_real_dripping_http_thread_then_preserves_raw(root, dripping_gateway, monkeypatch):
    port, started, disconnected, stats = dripping_gateway
    monkeypatch.setenv("DICTATION_DEADLINE_TEST_KEY", "local-test-only")
    gateway = CodexLBSettings(base_url=f"http://127.0.0.1:{port}", base_url_env="", api_key_env="DICTATION_DEADLINE_TEST_KEY")
    cleaner = Cleanup(gateway, timeout=.6)
    returned = threading.Event()
    def factory(*args):
        def call(*args):
            try:
                return cleaner(*args)
            finally:
                returned.set()
        return call
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        state = await completed(service)
        task = asyncio.create_task(service.cleanup(state["id"], "owner", CleanupRequest(scope="failed")))
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(.05)
        assert not closing.done() and not returned.is_set()
        await asyncio.wait_for(closing, 2)
        assert returned.is_set() and task.cancelled()
        assert service.storage.load(state["id"])["utterances"][0]["text"] == "原稿"
    asyncio.run(run())
    assert stats["chunks"] >= 3 and disconnected.wait(1)


def test_cleanup_curl_keeps_completed_output_and_provider_usage(dripping_gateway, monkeypatch):
    port, *_ = dripping_gateway
    monkeypatch.setenv("DICTATION_DEADLINE_TEST_KEY", "local-test-only")
    gateway = CodexLBSettings(base_url=f"http://127.0.0.1:{port}", base_url_env="", api_key_env="DICTATION_DEADLINE_TEST_KEY",
                             codex_responses_path="/complete")
    result = Cleanup(gateway, timeout=1)("原稿", [], [])
    assert result["text"] == "原稿。" and not result["needs_review"]
    assert result["metrics"]["reported_model"] == "local-test"
    assert result["metrics"]["input_tokens"] == 7 and result["metrics"]["output_tokens"] == 5


@pytest.mark.parametrize("timeout", [None, 0])
def test_dictation_curl_cannot_disable_its_total_deadline(timeout):
    client = SessionGatewayClient(CodexLBSettings(), timeout_seconds=timeout, credential="local-test-only")
    with pytest.raises(ValueError, match="总请求期限"):
        client.codex_responses_text({"stream": True})


def test_cleanup_usage_observer_records_provider_metadata_without_raw_files():
    from src.dictation.cleanup import UsageTrace
    trace = UsageTrace()
    trace.record_transport({"result": "ok"}, raw_response=(
        'event: response.completed\ndata: {"type":"response.completed","response":{"model":"reported-id","usage":{"input_tokens":53,"output_tokens":22}}}\n\n'
        'data: [DONE]\n\n'))
    assert trace.metadata == {"input_tokens": 53, "output_tokens": 22, "reported_model": "reported-id"}
    assert trace.write_text("raw.sse", "not persisted")


def test_document_chunks_cover_every_character():
    text = ("第一段。\n第二段可能包含否定，不能丢掉。" * 300) + "最后一字"
    chunks = list(document_chunks(text))
    assert "".join(chunks) == text
    assert all(len(chunk) <= 1600 for chunk in chunks)
    assert chunks[-1].endswith("最后一字")


def test_owner_gpu_lock_and_restart(root):
    async def run():
        service = DictationService(root)
        state = await service.create("owner", SessionRequest(), start=False)
        with pytest.raises(KeyError):
            service.get(state["id"], "other")
        with pytest.raises(Conflict, match="GPU"):
            await service.create("other", SessionRequest(), start=False)
        service.runtimes[state["id"]].release_gpu()
        recovered = DictationService(root)
        assert recovered.get(state["id"], "owner")["status"] == "interrupted"
    asyncio.run(run())

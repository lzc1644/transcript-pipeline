"""Event-loop-owned session state; worker processes never write the draft."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import struct
import time
from uuid import uuid4
import wave

from src.config_loader import load_settings
from src.codex_lb_client import CodexLBClientError
from src.dictation.cleanup import Cleanup, DEFAULT_CLEANUP_TIMEOUT_SECONDS
from src.dictation import document
from src.dictation.schemas import SessionRequest, load_dictation_settings
from src.dictation.storage import ACTIVE, Storage, atomic_json, public_state
from src.dictation.scheduler import Conflict, RecognitionBusy, RecognitionScheduler

RATE = 16000


def now():
    return datetime.now(timezone.utc).isoformat()


class UploadLimit(ValueError):
    pass


class WorkerIPCError(RuntimeError):
    pass


class CleanupFailed(RuntimeError):
    def __init__(self, message, *, timed_out=False):
        super().__init__(message)
        self.timed_out = timed_out


class Worker:
    WRITE_TIMEOUT_SECONDS = 3

    def __init__(self, interpreter, mode, config, root, log_path, lock_fd=None):
        self.interpreter, self.mode, self.config = interpreter, mode, config
        self.root, self.log_path, self.lock_fd = root, log_path, lock_fd
        self.process = None
        self.log = None
        self.write_lock = asyncio.Lock()

    async def start(self):
        self.log = self.log_path.open("ab")
        self.process = await asyncio.create_subprocess_exec(
            self.interpreter, "-m", "src.dictation.worker", self.mode, "--config", str(self.config),
            cwd=self.root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.log, limit=1024 * 1024,
            pass_fds=(() if self.lock_fd is None else (self.lock_fd,)),
            env={**os.environ, "HF_HUB_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
        event = await asyncio.wait_for(self.read(), 120)
        if event["type"] != "ready":
            raise RuntimeError(event.get("error", "模型启动失败"))
        return event.get("metadata", {})

    async def send(self, value):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError(f"{self.mode} worker 已退出")
        try:
            # One deadline covers BOTH lock acquisition and pipe backpressure.
            async with asyncio.timeout(self.WRITE_TIMEOUT_SECONDS):
                async with self.write_lock:
                    self.process.stdin.write((json.dumps(value) + "\n").encode())
                    await self.process.stdin.drain()
        except TimeoutError as exc:
            raise WorkerIPCError(f"{self.mode} worker IPC 写入超过期限；已停止接收新音频") from exc

    async def read(self):
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"{self.mode} worker 意外退出；请查看会话 worker 日志")
        return json.loads(line)

    async def close(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.stdin.close()
                await asyncio.wait_for(self.process.wait(), 3)
            except (asyncio.TimeoutError, BrokenPipeError):
                if self.process.returncode is None:
                    try:
                        self.process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    await self.process.wait()
        if self.log:
            self.log.close()


class Runtime:
    CPU_DRAIN_TIMEOUT_SECONDS = 10

    def __init__(self, manager, state):
        self.manager, self.state = manager, state
        self.asr_reserved = False
        self.cpu = None
        self.task = None
        self.queue = asyncio.Queue(maxsize=16)
        self.cleanup_queue = asyncio.Queue(maxsize=8)
        self.events = deque(maxlen=256)
        self.changed = asyncio.Event()
        self.processed = 0
        self.cancelled = False
        self.failure = None
        self.failure_signal = asyncio.Event()
        self.draining_started_at = None
        self.cpu_drained = False
        self.cpu_input_closed = False
        self.attached = False
        self.ever_attached = False
        self.ready_at = None
        self.disconnected_at = None
        self.last_audio_at = time.monotonic()
        self.last_voice_sample = 0
        self.audio_clock = None
        self.audio_spans = []
        self.new_audio_span = True
        self.idle_start_sample = 0
        self.pausing_started_at = None
        self.pause_started_at = None
        self.pause_boundary = None
        self.packet_lock = asyncio.Lock()
        self.cleanup_tasks = []
        self.dispatch_open = asyncio.Event()
        self.dispatch_open.set()
        self.file_inflight = set()
        self.file_resume_status = "listening"
        self.file_stopped = False

    def emit(self, kind, **data):
        state = self.state
        state["event_sequence"] += 1
        event = {"type": kind, "session_id": state["id"], "utterance_id": None,
                 "revision": state["revision"], "event_sequence": state["event_sequence"], **data}
        self.events.append(event)
        if kind != "asr.partial" and kind != "audio.ack":
            self.manager.storage.record(state["id"], event)
        self.changed.set()
        return event

    def save(self):
        pending = [u for u in self.state["utterances"] if u["status"] == "finalizing"]
        metrics = self.state["metrics"]
        metrics["asr_pending_utterances"] = len(pending)
        metrics["asr_audio_backlog_seconds"] = sum((u["end_sample"] - u["start_sample"]) / RATE for u in pending)
        metrics["max_asr_pending_utterances"] = max(len(pending), metrics.get("max_asr_pending_utterances", 0))
        metrics["max_asr_audio_backlog_seconds"] = max(metrics["asr_audio_backlog_seconds"], metrics.get("max_asr_audio_backlog_seconds", 0))
        self.manager.commit(self.state)

    def utterance(self, uid):
        for u in self.state["utterances"]:
            if u["id"] == uid:
                return u
        raise KeyError("语句不存在")

    async def run(self):
        state, manager = self.state, self.manager
        cfg = state["worker_config"]
        directory = manager.storage.path(state["id"])
        config_path = directory / "worker.json"
        jobs = []
        starting = time.monotonic()
        try:
            atomic_json(config_path, cfg)
            self.cpu = manager.worker_factory(cfg["cpu_python"], "cpu", config_path, manager.root, directory / "cpu.log")
            state.update(status="preparing", phase="准备 CPU 预览和 GPU 最终识别模型")
            self.save()
            loading = [asyncio.create_task(self.cpu.start()), asyncio.create_task(manager.recognition.start(self))]
            jobs.extend(loading)
            cpu_metadata, final_metadata = await asyncio.gather(*loading)
            state["metrics"].update(cpu_worker=cpu_metadata, final_worker=final_metadata)
            if self.failure:
                raise RuntimeError(self.failure)
            if self.cancelled:
                return
            state["metrics"]["startup_seconds"] = time.monotonic() - starting
            state.update(status="listening", phase="正在听音" if state["mode"] == "live" else "正在转录上传录音")
            self.ready_at = self.last_audio_at = time.monotonic()
            self.save()
            self.emit("session.ready")
            self.cleanup_tasks = [asyncio.create_task(self.cleanup_loop())]
            final_task = asyncio.create_task(self.final_loop())
            cpu_task = asyncio.create_task(self.cpu_loop())
            watchdog = asyncio.create_task(self.watchdog())
            failed = asyncio.create_task(self.failure_signal.wait())
            jobs.extend([final_task, cpu_task, watchdog, failed])
            if state["mode"] == "upload":
                producer = asyncio.create_task(self.feed_upload())
                jobs.append(producer)
            # Fail fast if either worker dies. GPU must be released before remote cleanup drains.
            async def core():
                await asyncio.gather(cpu_task, final_task, *([producer] if state["mode"] == "upload" else []))
            core_task = asyncio.create_task(core())
            jobs.append(core_task)
            done, _ = await asyncio.wait([core_task, watchdog, failed], return_when=asyncio.FIRST_COMPLETED)
            if self.failure:
                raise RuntimeError(self.failure)
            for job in done:
                if job.exception():
                    raise job.exception()
            await core_task
            watchdog.cancel()
            await self.release_recognition()
            state["phase_key"] = "cleaning"
            if state["status"] == "draining":
                state["phase"] = "正在完成逐句整理"
                self.save()
            await self.cleanup_queue.put(None)
            await asyncio.gather(*self.cleanup_tasks)
            if not self.cancelled:
                state.update(status="interrupted" if state.get("interrupted") else "completed", phase_key="completed",
                             phase="连接中断，已接收音频处理完成" if state.get("interrupted") else "已停止，处理完成")
                self.save()
                self.emit("session.completed")
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except Exception as exc:
            state.update(status="failed", phase="会话处理失败，已接收音频保留", error=str(exc))
            self.save()
            self.emit("session.error", error=str(exc))
        finally:
            for job in jobs + self.cleanup_tasks:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, *self.cleanup_tasks, return_exceptions=True)
            if self.cpu:
                await self.cpu.close()
            await self.release_recognition()
            for u in state["utterances"]:
                if u["status"] in {"partial", "finalizing"}:
                    u.update(status="asr_failed", needs_review=True,
                             error=self.failure or state.get("error") or "会话中断，最终识别未完成")
                    u.pop("asr_queue_state", None)
                elif u["status"] == "cleaning":
                    u.update(status="cleanup_failed", error="整理未完成，原始文字可用")
            if self.cancelled:
                state.update(status="cancelled", phase="已取消，已完成结果保留")
            self.save()
            self.changed.set()

    async def release_recognition(self):
        await self.manager.recognition.release(self)

    async def release_if_asr_done(self):
        if self.cpu_drained and not any(u["status"] in {"partial", "finalizing"} for u in self.state["utterances"]):
            await self.release_recognition()

    async def cpu_loop(self):
        while True:
            event = await self.cpu.read()
            kind = event["type"]
            if kind == "processed":
                self.processed = event["samples"]
                self.last_voice_sample = event["last_voice_sample"]
            elif kind == "utterance.start":
                self.state["utterances"].append({"id": event["utterance_id"], "revision": 0,
                    "status": "partial", "text": "", "raw_text": "", "preview": "", "edited": False,
                    "start_sample": event["start"], "end_sample": None, "needs_review": False})
            elif kind == "partial":
                u = self.utterance(event["utterance_id"])
                u["preview"] = event["text"]
                if self.audio_clock is not None and "first_partial_seconds" not in u:
                    u["first_partial_seconds"] = time.monotonic() - self.sample_time(u["start_sample"], endpoint=False)
                self.emit("asr.partial", utterance_id=u["id"], text=event["text"], revision=u["revision"])
            elif kind == "utterance.end":
                u = self.utterance(event["utterance_id"])
                u.update(end_sample=event["end"], boundary=event["boundary"], preview=event["preview"],
                         status="finalizing", last_voice_sample=event["last_voice"],
                         needs_review=event["boundary"] == "hard_limit")
                u["revision"] += 1
                path = self.manager.storage.utterance_audio(self.state["id"], u["id"], u["start_sample"], u["end_sample"])
                u["endpoint_at"] = time.monotonic()
                if self.audio_clock is not None:
                    u["endpoint_latency_seconds"] = u["endpoint_at"] - self.sample_time(u["last_voice_sample"])
                self.save()
                self.emit("utterance.pending", utterance_id=u["id"], revision=u["revision"])
                if self.queue.full() and self.state["mode"] == "live":
                    self.emit("session.warning", message="识别队列已满，停止新增采音；已接收音频继续处理")
                    await self.stop("识别队列已满，已停止新增采音，正在完成已接收音频")
                await self.queue.put((u["id"], path, u["endpoint_at"]))
            elif kind == "paused":
                if event["samples"] != self.state["samples"]:
                    raise RuntimeError("CPU 暂停位置与持久音频不一致，已接收音频保留")
                if self.state["status"] == "pausing" and not self.cancelled:
                    self.state.update(status="paused", phase="已暂停 · 麦克风已关闭，会话名额保留")
                    self.state["pause_boundaries"][-1]["confirmed_at"] = now()
                    self.save()
                    self.emit("session.paused")
            elif kind == "drained":
                self.cpu_drained = self.cpu_input_closed = True
                await self.queue.put(None)
                await self.release_if_asr_done()
                return
            elif kind == "decoded":
                self.state.update(decoded=True, audio_total_samples=event["samples"])
                self.save()
            elif kind == "error":
                raise RuntimeError(event["error"])

    async def final_loop(self):
        while True:
            item = await self.queue.get()
            if item is None:
                if self.state["mode"] == "upload":
                    self.state["asr_processed_samples"] = min(self.state["samples"], self.state.get("audio_total_samples", self.processed))
                    self.save()
                    self.update_file_pause()
                return
            uid, path, queued = item
            u = self.utterance(uid)
            try:
                result = await self.manager.recognition.transcribe(self, uid, path, queued)
            finally:
                self.end_dispatch("asr")
            if self.cancelled or self.manager.runtimes.get(self.state["id"]) is not self:
                return
            u.pop("asr_queue_state", None)
            if result["type"] in {"final", "failed"}:
                self.state["asr_processed_samples"] = max(self.state.get("asr_processed_samples", 0), u["end_sample"])
            if result["type"] == "final":
                u.update(raw_text=result["text"], status="raw", asr_seconds=result["inference_seconds"],
                         queue_wait_seconds=result["queue_wait_seconds"])
                if "gpu_peak_allocated_bytes" in result:
                    self.state["metrics"].update(gpu_peak_allocated_bytes=result["gpu_peak_allocated_bytes"],
                                                 gpu_allocated_bytes=result["gpu_allocated_bytes"])
                if not u["edited"]:
                    u["text"] = result["text"]
                u["revision"] += 1
                if self.audio_clock is not None:
                    u["final_latency_seconds"] = time.monotonic() - self.sample_time(u["last_voice_sample"])
                self.manager.storage.artifact(self.state["id"], "asr", uid, result)
                if self.state.get("editor_version") == 1:
                    self.manager.commit_document(self.state, lambda draft: document.insert_utterance(draft, u))
                self.save()
                self.emit("asr.final", utterance_id=uid, text=result["text"], revision=u["revision"])
                await self.release_if_asr_done()
                if self.state["config"]["cleanup"]:
                    if self.cleanup_queue.full() and self.state["mode"] == "live":
                        u.update(status="cleanup_failed", error="整理队列已满，原始文字可用；停止后可重试")
                        self.save()
                    else:
                        u["status"] = "cleaning"
                        self.save()
                        await self.cleanup_queue.put((uid, u["revision"]))
            elif result["type"] == "failed":
                u.update(status="asr_failed", error=result["error"], needs_review=True)
                self.save()
                self.emit("utterance.failed", utterance_id=uid, stage="asr", error=result["error"])
                await self.release_if_asr_done()
            else:
                raise RuntimeError(result.get("error", "最终识别 worker 协议无效"))
            self.update_file_pause()

    async def cleanup_loop(self):
        while True:
            item = await self.cleanup_queue.get()
            if item is None:
                return
            uid, revision = item
            await self.manager.clean_utterance(self.state, uid, revision, self)
            self.update_file_pause()

    async def wait_dispatch(self):
        if self.state["mode"] == "upload":
            while not self.dispatch_open.is_set():
                await self.dispatch_open.wait()
        if self.cancelled:
            raise asyncio.CancelledError

    def try_begin_dispatch(self, stage):
        if self.cancelled:
            raise asyncio.CancelledError
        if self.state["mode"] == "upload":
            if not self.dispatch_open.is_set():
                return False
            self.file_inflight.add(stage)
        return True

    async def begin_dispatch(self, stage):
        while True:
            await self.wait_dispatch()
            if self.try_begin_dispatch(stage):
                return

    def end_dispatch(self, stage):
        self.file_inflight.discard(stage)
        self.update_file_pause()

    def file_work_remaining(self):
        return (not self.cpu_drained or bool(self.file_inflight) or
                any(u["status"] in {"partial", "finalizing", "cleaning"} for u in self.state["utterances"]))

    def update_file_pause(self):
        if self.state["mode"] != "upload" or self.state["status"] != "pausing" or self.file_inflight:
            return
        if not self.file_work_remaining():
            self.state.update(status=self.file_resume_status, phase="处理已结束，正在提交结果")
            self.dispatch_open.set()
        else:
            self.state.update(status="paused", phase="转写已暂停 · 会话名额保留，不独占 GPU")
        self.save()
        self.emit("session.progress")

    def pause_file(self):
        if self.state["mode"] != "upload":
            raise Conflict("实时暂停须在采音窗口通过有序 WebSocket 操作")
        if self.state["status"] in {"paused", "pausing"} or not self.file_work_remaining():
            return
        if self.state["status"] not in {"listening", "draining"} or self.cancelled:
            raise Conflict("文件尚未准备好或已结束，不能暂停")
        self.file_resume_status = self.state["status"]
        self.dispatch_open.clear()
        self.state.update(status="pausing", phase="正在暂停，等待已开始的解码/识别/整理完成")
        self.save()
        self.update_file_pause()

    def resume_file(self):
        if self.state["mode"] != "upload":
            raise Conflict("实时继续须返回采音窗口")
        if self.state["status"] == "listening" or self.state["status"] == "draining":
            return
        if self.state["status"] != "paused" or self.cancelled:
            raise Conflict("只有已确认暂停的文件转写才能继续")
        self.state.update(status=self.file_resume_status, phase="继续转写录音" if self.file_resume_status == "listening" else "继续完成尾部及整理")
        if self.draining_started_at is not None:
            self.draining_started_at = time.monotonic()
        self.dispatch_open.set()
        self.manager.recognition.wake.set()
        self.save()
        self.emit("session.resumed")

    async def send_cpu(self, command):
        try:
            await self.cpu.send(command)
        except (WorkerIPCError, BrokenPipeError, ConnectionError, RuntimeError) as exc:
            self.failure = str(exc)
            self.failure_signal.set()
            raise WorkerIPCError(str(exc)) from exc

    async def accept_audio(self, packet: bytes):
        async with self.packet_lock:
            if len(packet) < 14 or len(packet) > 12 + 6400 or len(packet) % 2:
                raise ValueError("音频包要求序号(uint32)、样本起点(uint64)、PCM16，最长200ms")
            seq, start = struct.unpack_from("<IQ", packet)
            pcm = packet[12:]
            state = self.state
            if seq < state["next_sequence"]:
                previous = state["packets"].get(str(seq))
                if previous != [start, len(pcm) // 2] or not self.manager.storage.same_audio(state["id"], start, pcm):
                    raise Conflict("重复音频包内容不同或超过恢复窗口，必须中断")
                return self.emit("audio.ack", sequence=seq, next_sequence=state["next_sequence"], samples=state["samples"])
            if state["status"] != "listening" or self.failure or self.cancelled:
                raise Conflict("会话不再接收录音")
            if seq != state["next_sequence"] or start != state["samples"]:
                raise Conflict("音频序号或样本起点不连续，拒绝带缺口续录")
            lag = (state["samples"] - self.processed) / RATE
            state["metrics"]["backlog_seconds"] = lag
            state["metrics"]["max_backlog_seconds"] = max(lag, state["metrics"].get("max_backlog_seconds", 0))
            if seq % 375 == 0:  # 30 seconds of 80ms transport packets
                checkpoints = state["metrics"].setdefault("backlog_checkpoints", [])
                checkpoints.append({"sample": state["samples"], "seconds": lag})
                del checkpoints[:-256]
            if lag > 5:
                await self._stop("处理积压超过5秒，已停止新增采音")
                raise Conflict("处理积压超过5秒；已接收音频将继续处理")
            if lag > 2:
                self.emit("session.warning", message="音频积压超过2秒")
            self.manager.storage.append_audio(state["id"], pcm)
            state["packets"][str(seq)] = [start, len(pcm) // 2]
            if len(state["packets"]) > 128:
                del state["packets"][next(iter(state["packets"]))]
            state["next_sequence"] += 1
            state["samples"] += len(pcm) // 2
            if state["mode"] == "upload":
                state["upload_cursor_samples"] = state["samples"]
            self.last_audio_at = time.monotonic()
            if self.new_audio_span and state["mode"] == "live":
                clock = self.last_audio_at - len(pcm) / 2 / RATE
                self.audio_spans.append((start, clock))
                self.new_audio_span = False
                if self.audio_clock is None:
                    self.audio_clock = clock
            self.save()
            await self.send_cpu({"op": "audio", "pcm": base64.b64encode(pcm).decode()})
            return self.emit("audio.ack", sequence=seq, next_sequence=state["next_sequence"], samples=state["samples"])

    def check_boundary(self, next_sequence, samples):
        if (type(next_sequence) is not int or type(samples) is not int
                or (next_sequence, samples) != (self.state["next_sequence"], self.state["samples"])):
            raise Conflict("控制边界与已持久化音频不一致，不能跳过未确认音频")
        if (self.cancelled or self.manager.closing or self.state["id"] in self.manager.deleting
                or self.state["mode"] != "live"):
            raise Conflict("会话不可恢复")

    async def pause(self, next_sequence, samples):
        async with self.packet_lock:
            self.check_boundary(next_sequence, samples)
            if self.state["status"] in {"pausing", "paused"}:
                self.emit("session." + self.state["status"])
                return
            if self.state["status"] != "listening":
                raise Conflict("会话已停止，不能暂停")
            self.pause_boundary = (next_sequence, samples)
            self.pausing_started_at = self.pause_started_at = time.monotonic()
            self.state.setdefault("pause_boundaries", []).append({"next_sequence": next_sequence,
                "samples": samples, "requested_at": now()})
            self.state.update(status="pausing", phase="正在暂停，完成暂停前的尾句")
            self.save()
            self.emit("session.pausing")
            await self.send_cpu({"op": "pause"})

    async def resume(self, next_sequence, samples, document_version=None, insertion_position=None):
        async with self.packet_lock:
            self.check_boundary(next_sequence, samples)
            if self.state["status"] == "listening" and self.pause_boundary == (next_sequence, samples):
                self.emit("session.resumed")
                return
            if self.state["status"] != "paused":
                raise Conflict("只有已确认暂停的会话才能继续；请返回录音窗口检查状态")
            notebook = self.state.get("editor_version") == 1
            if notebook:
                if self.state.get("document_suggestion"):
                    raise Conflict("请先应用或放弃全文整理预览")
                if document_version != self.state["document_version"]:
                    raise Conflict("正文位置已更新，请同步后再继续")
            resumed = time.monotonic()
            def resume_state(candidate):
                if notebook:
                    document.start_turn(candidate, samples,
                        insertion_position if insertion_position is not None else document.length(candidate["document_text"]))
                candidate.update(status="listening", phase="正在听音")
                candidate["metrics"] = dict(candidate["metrics"])
                if self.pause_started_at is not None:
                    metrics = candidate["metrics"]
                    metrics["paused_wall_seconds"] = metrics.get("paused_wall_seconds", 0) + resumed - self.pause_started_at
                boundaries = [dict(b) for b in candidate["pause_boundaries"]]
                boundaries[-1]["resumed_at"] = now()
                candidate["pause_boundaries"] = boundaries
            if notebook:
                self.manager.commit_document(self.state, resume_state)
            else:
                resume_state(self.state)
                self.save()
            self.ready_at = self.last_audio_at = resumed
            self.idle_start_sample = samples
            self.new_audio_span = True
            self.pause_started_at = None
            self.emit("session.resumed")

    def sample_time(self, sample, *, endpoint=True):
        # Each capture span has its own wall clock. A result finishing after a
        # later resume still uses the span that actually contained its audio.
        for start, clock in reversed(self.audio_spans):
            if sample > start or (not endpoint and sample == start) or start == 0:
                return clock + (sample - start) / RATE
        return self.audio_clock + sample / RATE

    async def stop(self, reason="正在完成尾部识别和整理"):
        async with self.packet_lock:
            await self._stop(reason)

    async def _stop(self, reason):
        if self.state["status"] in {"created", "preparing", "waiting"}:
            await self.manager.cancel(self.state["id"], self.state["owner"])
            return
        if self.state["status"] not in {"listening", "pausing", "paused"} or self.cancelled:
            return
        self.file_stopped = True
        self.dispatch_open.set()
        self.manager.recognition.wake.set()
        self.state.update(status="draining", phase=reason)
        self.draining_started_at = time.monotonic()
        self.save()
        self.emit("session.draining", message=reason)
        await self.close_cpu_input()

    async def close_cpu_input(self):
        # Called under packet_lock. EOF and user stop share one close, while
        # reopening dispatch to finish queued ASR/cleanup is a separate action.
        if self.cpu_input_closed or self.cpu_drained:
            return
        self.cpu_input_closed = True
        await self.send_cpu({"op": "stop"})

    def silence_seconds(self):
        # Acoustic activity belongs only to VAD, never delayed ASR text.
        return max(0, self.processed - max(self.last_voice_sample, self.idle_start_sample)) / RATE

    def sample_resources(self):
        for name, worker in (("cpu", self.cpu), ("final_shared", self.manager.recognition.worker)):
            pid = getattr(getattr(worker, "process", None), "pid", None)
            if not pid:
                continue
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                rss = next(int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:"))
                stat = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
                cpu_seconds = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
                metric = self.state["metrics"].setdefault(name + "_process", {})
                metric.update(peak_rss_bytes=max(rss, metric.get("peak_rss_bytes", 0)), cpu_seconds=cpu_seconds)
            except (OSError, ValueError, StopIteration, IndexError):
                pass  # /proc is diagnostic only; a disappearing worker is handled by its reader.

    async def watchdog(self):
        while self.state["status"] in ACTIVE:
            await asyncio.sleep(1)
            self.sample_resources()
            clock = time.monotonic()
            if self.queue.full():
                # Shared inference backpressure is not a stalled CPU worker.
                if self.draining_started_at is not None:
                    self.draining_started_at = clock
                if self.pausing_started_at is not None:
                    self.pausing_started_at = clock
            if (self.state["status"] == "draining" and not self.cpu_drained
                    and self.draining_started_at is not None
                    and clock - self.draining_started_at >= self.CPU_DRAIN_TIMEOUT_SECONDS):
                self.failure = "停止后的 CPU 收尾超过期限，已接收音频保留"
                self.failure_signal.set()
                return
            if (self.state["status"] == "pausing" and self.pausing_started_at is not None
                    and clock - self.pausing_started_at >= self.CPU_DRAIN_TIMEOUT_SECONDS):
                self.failure = "暂停确认超过期限，已停止接收新音频；原始音频保留"
                self.failure_signal.set()
                return
            if self.state["mode"] != "live":
                continue
            cfg = self.state["worker_config"]
            # Paused is not idle, but a closed browser must still release GPU.
            if (self.state["status"] in {"listening", "pausing", "paused"} and self.disconnected_at
                    and clock - self.disconnected_at >= cfg["disconnect_seconds"]):
                self.state["interrupted"] = True
                await self.stop("网络中断超出恢复窗口，已停止监听")
            if self.state["status"] != "listening":
                continue
            idle = self.silence_seconds()
            if not self.ever_attached and self.state["samples"] == 0 and self.ready_at is not None:
                if clock - self.ready_at >= cfg["disconnect_seconds"]:
                    self.state["interrupted"] = True
                    await self.stop("未在期限内建立录音连接，已释放资源")
                    continue
            if idle >= cfg["idle_seconds"]:
                await self.stop("长时间无语音，自动停止监听")
            elif idle >= cfg["idle_seconds"] - 10:
                self.emit("session.warning", message="10秒内仍无语音将自动停止")
            if clock - self.last_audio_at > 15 and self.attached:
                await self.stop("未收到麦克风音频，已停止监听")

    async def feed_upload(self):
        path = self.manager.storage.path(self.state["id"])
        await self.begin_dispatch("decode")
        self.state.update(phase_key="decoding", phase="正在解码录音")
        self.save()
        try:
            await self.send_cpu({"op": "decode", "source": str(path / self.state["upload_name"]),
                                 "destination": str(path / "audio/decoded.wav"),
                                 "max_seconds": self.state["config"]["max_upload_minutes"] * 60})
            # Decode response is handled by cpu_loop; never create a second reader.
            deadline = time.monotonic() + self.state["worker_config"].get("decode_timeout_seconds", 900)
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError("录音解码超时")
                if self.state.get("decoded"):
                    break
                await asyncio.sleep(0.05)
        finally:
            self.end_dispatch("decode")
        self.state["phase_key"] = "transcribing"
        if self.state["status"] == "listening":
            self.state["phase"] = "正在转写录音"
        with wave.open(str(path / "audio/decoded.wav")) as source:
            while not self.file_stopped:
                while self.state["samples"] - self.processed > RATE and not self.file_stopped:
                    await asyncio.sleep(0.02)
                await self.begin_dispatch("audio")
                try:
                    if self.file_stopped:
                        break
                    pcm = source.readframes(1280)
                    if not pcm:
                        break
                    packet = struct.pack("<IQ", self.state["next_sequence"], self.state["samples"]) + pcm
                    await self.accept_audio(packet)
                    self.state["upload_cursor_samples"] = source.tell()
                finally:
                    self.end_dispatch("audio")
        async with self.packet_lock:
            if self.state["status"] in {"pausing", "paused"}:
                # Natural EOF closes only the CPU input, never the dispatch gate.
                self.file_resume_status = "draining"
                self.draining_started_at = time.monotonic()
                await self.close_cpu_input()
            else:
                await self._stop("正在完成尾部识别和整理")


class DictationService:
    UPLOAD_RECEIVE_TIMEOUT_SECONDS = 1800
    CLEANUP_CONCURRENCY = 8

    def __init__(self, root: Path, *, worker_factory=Worker, cleanup_factory=Cleanup, recover=True):
        self.root, self.storage = root, Storage(root)
        self.worker_factory, self.cleanup_factory = worker_factory, cleanup_factory
        self.runtimes = {}
        self.states = {}
        self.projections = {}
        self.background = {}
        self.document_cleanups = set()
        self.deleting = set()
        self.closing = False
        self.gate = asyncio.Lock()
        self.cleanup_slots = asyncio.Semaphore(self.CLEANUP_CONCURRENCY)
        self.recognition = RecognitionScheduler(self)
        if recover:
            self.storage.recover()

    def get(self, sid, owner):
        if self.closing or sid in self.deleting:
            raise Conflict("服务正在停止或会话正在删除，不再接受新操作")
        # Every live/recovered session has exactly one in-process state owner.
        if sid not in self.states:
            self.states[sid] = self.storage.load(sid)
            self.projections[sid] = self.storage.render_draft(self.states[sid])
        state = self.states[sid]
        if state["owner"] != owner:
            raise KeyError("会话不存在")
        return state

    def commit(self, state, *, event=None, uid=None):
        # All callers are on the app event loop; there is deliberately no await
        # between validating a revision, mutating state and persisting it.
        if state["id"] in self.deleting:
            return
        draft = self.storage.render_draft(state)
        if draft != self.projections.get(state["id"], ""):
            state["content_revision"] = state.get("content_revision", 0) + 1
            state.pop("document_suggestion", None)
        self.projections[state["id"]] = draft
        state["revision"] += 1
        state["updated_at"] = now()
        runtime = self.runtimes.get(state["id"])
        notification = None
        if event:
            state["event_sequence"] += 1
            notification = {"type": event, "session_id": state["id"], "utterance_id": uid,
                "revision": next(u["revision"] for u in state["utterances"] if u["id"] == uid) if uid else state["revision"],
                "event_sequence": state["event_sequence"]}
        self.storage.save(state)
        if notification:
            try:
                self.storage.record(state["id"], notification)
            except OSError:
                # A diagnostic log failure cannot reverse an acknowledged body.
                logging.getLogger(__name__).exception("会话已保存，但状态事件日志写入失败")
            if runtime:
                runtime.events.append(notification)
                runtime.changed.set()

    def list(self, owner):
        return sorted([public_state(s) for s in self.storage.all() if s["owner"] == owner],
                      key=lambda s: s["created_at"], reverse=True)

    async def create(self, owner, request: SessionRequest, *, upload_name=None, start=True):
        async with self.gate:
            if self.closing:
                raise Conflict("服务正在停止，不再接受新会话")
            settings = load_dictation_settings(self.root)
            if not settings.enabled:
                raise Conflict("语音输入模块已关闭")
            loaded = load_settings(project_root=self.root)
            config = settings.freeze(self.root, loaded, request, preview=not upload_name)
            for key in ("cpu_python", "final_python", "vad_model"):
                if not Path(config[key]).is_file():
                    raise Conflict(f"语音输入尚未准备好：{key} 不存在，请查看 docs/DICTATION.md")
            if not upload_name and not (Path(config["streaming_model"]) / "encoder.int8.onnx").is_file():
                raise Conflict("流式模型尚未预下载，请运行 scripts/setup_dictation.py")
            sid = uuid4().hex
            state = {"id": sid, "owner": owner, "status": "created", "phase": "等待准备模型",
                     "mode": "upload" if upload_name else "live", "created_at": now(), "updated_at": now(),
                     "revision": 0, "event_sequence": 0, "samples": 0, "next_sequence": 0, "packets": {},
                     "utterances": [], "metrics": {}, "worker_config": config,
                     "config": {k: config[k] for k in ("terms", "cleanup", "cleanup_model", "reasoning_effort",
                         "document_cleanup_model", "document_reasoning_effort",
                         "cleanup_timeout_seconds", "document_cleanup_timeout_seconds", "decode_timeout_seconds",
                         "streaming_model_id", "streaming_revision", "qwen_model", "qwen_revision",
                         "max_upload_mb", "max_upload_minutes", "segmentation")}}
            if request.editor_version == 1:
                state["document_text"] = request.initial_text
                document.enable(state, self.storage.render_draft)
                state["dictation_turns"][0]["position"] = (request.insertion_position if request.insertion_position is not None
                    else document.length(request.initial_text))
                document.index(request.initial_text, state["dictation_turns"][0]["position"])
            if upload_name:
                state["upload_name"] = upload_name
            from src.web.frontend_settings import load_frontend_settings
            frontend = load_frontend_settings(self.root)
            gateway = loaded.settings.codex_lb
            state["gateway"] = gateway.model_copy(update={
                "base_url": os.getenv(gateway.base_url_env, "") or frontend.codex_lb_base_url or gateway.base_url,
                "base_url_env": "",
            }).model_dump()
            runtime = Runtime(self, state)
            if not upload_name:
                await self.recognition.reserve(runtime, settings.max_active_sessions)
            try:
                self.storage.create(state)
                self.states[sid] = state
                self.projections[sid] = ""
                self.runtimes[sid] = runtime
                if start:
                    runtime.task = asyncio.create_task(runtime.run())
            except BaseException:
                await runtime.release_recognition()
                raise
            return state

    async def receive_upload(self, sid, owner, chunks, content_length=0):
        state = self.get(sid, owner)
        runtime = self.runtimes[sid]
        task = asyncio.current_task()
        self.background.setdefault(sid, set()).add(task)
        maximum = state["config"]["max_upload_mb"] * 1024 * 1024
        try:
            if content_length > maximum:
                raise UploadLimit("上传录音超过大小上限")
            destination = self.storage.path(sid) / state["upload_name"]
            count = 0
            async with asyncio.timeout(self.UPLOAD_RECEIVE_TIMEOUT_SECONDS):
                with destination.open("xb") as output:
                    async for chunk in chunks:
                        if count + len(chunk) > maximum:
                            raise UploadLimit("上传录音超过大小上限")
                        output.write(chunk)
                        count += len(chunk)
            if not count:
                raise ValueError("上传文件为空")
            # start_upload rechecks cancellation when it reserves recognition.
            if (self.runtimes.get(sid) is not runtime or runtime.cancelled
                    or sid in self.deleting or self.closing):
                raise Conflict("上传会话已取消或正在删除")
            state["upload_bytes"] = count
            self.commit(state)
            await self.start_upload(sid, owner, wait_if_busy=True)
        except BaseException as exc:
            runtime.cancelled = True
            await runtime.release_recognition()
            state.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                         phase="上传已取消" if isinstance(exc, asyncio.CancelledError) else "上传失败",
                         error=("上传未完成" if isinstance(exc, asyncio.CancelledError) else
                                "上传接收超过期限，请重新上传" if isinstance(exc, TimeoutError) else str(exc)))
            self.commit(state)
            raise
        finally:
            self.background[sid].discard(task)
        return state

    async def start_upload(self, sid, owner, *, wait_if_busy=False):
        async with self.gate:
            state = self.get(sid, owner)
            runtime = self.runtimes.get(sid)
            if (not runtime or runtime.cancelled or state["mode"] != "upload"
                    or state["status"] not in {"created", "waiting"} or not state.get("upload_bytes")):
                raise Conflict("上传尚未完成或会话不可启动")
            settings = load_dictation_settings(self.root)
            if not settings.enabled:
                raise Conflict("语音输入模块已关闭")
            try:
                await self.recognition.reserve(runtime, settings.max_active_sessions)
            except RecognitionBusy as exc:
                state.update(status="waiting", phase=f"{exc}；文件已保留，请重试开始转写", wait_reason=exc.reason)
                self.commit(state)
                if not wait_if_busy:
                    raise
                return state
            state.pop("wait_reason", None)
            state.update(status="created", phase="等待准备模型")
            self.commit(state)
            runtime.task = asyncio.create_task(runtime.run())
            return state

    def gateway(self, state):
        from src.schemas import CodexLBSettings
        return CodexLBSettings.model_validate(state["gateway"])

    def cleaner(self, state, *, settings=None):
        cfg = state["worker_config"]
        timeout = settings.cleanup_timeout_seconds if settings else cfg["cleanup_timeout_seconds"]
        document_timeout = settings.document_cleanup_timeout_seconds if settings else cfg.get(
            "document_cleanup_timeout_seconds", DEFAULT_CLEANUP_TIMEOUT_SECONDS)
        kwargs = {"project_root": self.root,
                  "document_model": cfg.get("document_cleanup_model", "gpt-6-luna"),
                  "reasoning_effort": cfg.get("reasoning_effort", "low"),
                  "document_reasoning_effort": cfg.get("document_reasoning_effort", "high"),
                  "document_timeout": document_timeout} if self.cleanup_factory is Cleanup else {}
        return self.cleanup_factory(self.gateway(state), cfg["cleanup_model"], timeout, **kwargs)

    async def external(self, function, *args, runtime=None):
        while True:
            if runtime:
                await runtime.wait_dispatch()  # Never reserve a global slot while paused.
            await self.cleanup_slots.acquire()
            try:
                if not runtime or runtime.try_begin_dispatch("cleanup"):
                    break  # Gate recheck and request registration are one event-loop turn.
            except BaseException:
                self.cleanup_slots.release()
                raise
            self.cleanup_slots.release()
        try:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep the slot until the bounded transport actually exits.
                await asyncio.gather(task, return_exceptions=True)
                raise
        finally:
            self.cleanup_slots.release()
            if runtime:
                runtime.end_dispatch("cleanup")

    async def clean_utterance(self, state, uid, revision, runtime=None):
        u = next(u for u in state["utterances"] if u["id"] == uid)
        context = [v["text"] for v in state["utterances"][:state["utterances"].index(u)] if v.get("raw_text")][-2:]
        cfg = state["worker_config"]
        try:
            # Manual retries use current deadlines without rewriting an old snapshot.
            cleaner = self.cleaner(state, settings=load_dictation_settings(self.root) if runtime is None else None)
            result = await self.external(cleaner, u["raw_text"], context, cfg["terms"], runtime=runtime)
            if runtime and runtime.cancelled:
                return
            self.storage.artifact(state["id"], "cleaned", uid, result)
            u.update(cleaned_text=result["text"],
                     needs_review=result["needs_review"] or u.get("boundary") == "hard_limit", status="cleaned")
            u.pop("error", None)
            can_apply = not u["edited"] and u["revision"] == revision
            if state.get("editor_version") == 1:
                can_apply = can_apply and self.commit_document(state,
                    lambda draft: document.clean_utterance(draft, u, result["text"]))
            if can_apply:
                u["text"] = result["text"]
                u.pop("suggestion", None)
            else:
                u["suggestion"] = result["text"]
            u["revision"] += 1
            if runtime and runtime.audio_clock is not None:
                u["cleanup_latency_seconds"] = time.monotonic() - runtime.sample_time(u["last_voice_sample"])
            self.commit(state, event="cleanup.completed", uid=uid)
        except Exception as exc:
            u.update(status="cleanup_failed", error=str(exc), revision=u["revision"] + 1)
            self.commit(state, event="utterance.failed", uid=uid)

    def edit(self, sid, owner, uid, revision, text, discard_document=False):
        state = self.get(sid, owner)
        if state.get("editor_version") == 1:
            raise Conflict("此会话使用连续正文，请刷新并通过版本化正文接口编辑")
        u = next((u for u in state["utterances"] if u["id"] == uid), None)
        if not u:
            raise KeyError("语句不存在")
        if u["revision"] != revision:
            raise Conflict("文字版本已更新，请刷新后再保存；你的编辑未被覆盖")
        if not u.get("raw_text"):
            raise Conflict("最终识别完成后才可编辑")
        if state.get("document_text") is not None and not discard_document:
            raise Conflict("已应用全文稿；请编辑全文，或明确确认返回逐句稿")
        if discard_document:
            state.pop("document_text", None)
            state.pop("document_revision", None)
        u.update(text=text, edited=True, revision=revision + 1)
        self.commit(state, event="utterance.edited", uid=uid)
        return public_state(state)

    def edit_draft(self, sid, owner, revision, text):
        state = self.get(sid, owner)
        if state.get("editor_version") == 1:
            raise Conflict("此会话使用连续正文，请刷新并通过版本化正文接口编辑")
        if state["revision"] != revision:
            raise Conflict("正文已更新；你的编辑未被覆盖，请刷新后重试")
        if state["status"] in ACTIVE:
            raise Conflict("整篇编辑需等待停止后的处理完成")
        state.update(document_text=text, document_revision=revision + 1)
        self.commit(state, event="document.edited")
        return public_state(state)

    def commit_document(self, state, mutate, *, event=None):
        # document.py constructs new text/ranges/turns/logs rather than mutating
        # shared containers. Persist that candidate before publishing it to the
        # runtime; failed writes leave the live document and its version intact.
        candidate = dict(state)
        result = mutate(candidate)
        projection = self.projections.get(state["id"], "")
        try:
            self.commit(candidate, event=event)
        except OSError:
            self.projections[state["id"]] = projection
            raise
        state.clear()
        state.update(candidate)
        return result

    def enable_document(self, sid, owner, revision):
        state = self.get(sid, owner)
        if state.get("editor_version") != 1:
            if state["revision"] != revision:
                raise Conflict("会话已更新，请同步后打开正文")
            self.commit_document(state, lambda draft: document.enable(draft, self.storage.render_draft), event="document.enabled")
        return public_state(state)

    def edit_document(self, sid, owner, request):
        state = self.get(sid, owner)
        if state.get("editor_version") != 1:
            raise Conflict("请先打开连续正文编辑模式")
        self.commit_document(state, lambda draft: document.edit(draft, request.operation_id, request.base_version,
            [c.model_dump(by_alias=True) for c in request.changes]), event="document.edited")
        return public_state(state)

    def discard_suggestion(self, sid, owner, suggestion_id):
        state = self.get(sid, owner)
        suggestion = state.get("document_suggestion")
        if suggestion and suggestion["id"] == suggestion_id:
            state.pop("document_suggestion", None)
            self.commit(state, event="document.discarded")
        return public_state(state)

    async def cancel(self, sid, owner, *, internal=False):
        state = self.states[sid] if internal else self.get(sid, owner)
        runtime = self.runtimes.get(sid)
        if runtime:
            runtime.cancelled = True
        tasks = [t for t in self.background.get(sid, set()) if t is not asyncio.current_task()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        if runtime and runtime.task and not runtime.task.done():
            if not runtime.task.cancelling():
                runtime.task.cancel()
            await asyncio.shield(asyncio.gather(runtime.task, return_exceptions=True))
        elif state["status"] in ACTIVE:
            if runtime:
                runtime.cancelled = True
                await runtime.release_recognition()
            state.update(status="cancelled", phase="已取消")
            self.commit(state)
        return public_state(state)

    async def delete(self, sid, owner):
        self.get(sid, owner)
        # Claim deletion before the first await; no new edit/cleanup may join.
        self.deleting.add(sid)
        tasks = list(self.background.get(sid, set()))
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        await self.cancel(sid, owner, internal=True)
        self.storage.delete(sid)
        self.runtimes.pop(sid, None)
        self.states.pop(sid, None)
        self.projections.pop(sid, None)
        self.deleting.discard(sid)

    async def cleanup(self, sid, owner, request):
        self.get(sid, owner)
        document = request.scope == "document"
        if document and sid in self.document_cleanups:
            raise Conflict("本会话已有全文整理正在执行，请等待完成；原稿仍可使用")
        if sum(len(group) for group in self.background.values()) >= self.CLEANUP_CONCURRENCY:
            raise Conflict("已有整理/上传正在执行或队列已满，请等待；原始文字仍可用")
        task = asyncio.current_task()
        self.background.setdefault(sid, set()).add(task)
        if document:
            self.document_cleanups.add(sid)
        try:
            return await self._cleanup(sid, owner, request)
        finally:
            self.background[sid].discard(task)
            if document:
                self.document_cleanups.discard(sid)

    async def _cleanup(self, sid, owner, request):
        state = self.get(sid, owner)
        if state["status"] in ACTIVE:
            raise Conflict("请先停止并等待本次识别结束，再重试整理")
        if request.scope == "apply":
            if request.operation_id and any(op["id"] == request.operation_id for op in state.get("document_operations", [])):
                return public_state(state)
            suggestion = state.get("document_suggestion")
            notebook = state.get("editor_version") == 1
            if (not suggestion or suggestion["id"] != request.suggestion_id
                    or (request.base_content_revision != state.get("content_revision", 0) if notebook else request.revision != state["revision"])
                    or suggestion["base_content_revision"] != state.get("content_revision", 0)):
                raise Conflict("正文或建议版本已变化，请重新预览差异")
            text = request.text if notebook and request.text is not None else suggestion["text"]
            if notebook:
                def apply_preview(draft):
                    document.apply(draft, [{"from": 0, "to": document.length(draft["document_text"]), "insert": text}],
                                   operation_id=request.operation_id, kind="apply")
                    for source in draft["document_sources"]:
                        source["touched"] = True
                    draft["document_revision"] = draft["revision"] + 1
                    draft.pop("document_suggestion", None)
                self.storage.artifact(sid, "cleaned", "document-" + uuid4().hex,
                    {"suggestion_id": suggestion["id"], "original": suggestion["original"],
                     "model_text": suggestion["text"], "applied_text": text})
                self.commit_document(state, apply_preview, event="document.applied")
            else:
                state["document_text"] = text
                state["document_revision"] = state["revision"] + 1
                state.pop("document_suggestion", None)
                self.commit(state, event="document.applied")
            return public_state(state)
        if request.scope == "document":
            original = self.storage.render_draft(state)
            if not original.strip():
                raise Conflict("没有可整理的已完成文字")
            content_revision = state.get("content_revision", 0)
            cfg = state["worker_config"]
            state.pop("document_cleanup_error", None)
            state.pop("document_suggestion", None)
            self.commit(state, event="document.started")
            settings = load_dictation_settings(self.root)
            try:
                cleaner = self.cleaner(state, settings=settings)
                result = await self.external(cleaner.document, original, [], cfg["terms"])
            except (CodexLBClientError, ValueError, TimeoutError) as exc:
                timed_out = isinstance(exc, TimeoutError) or "curl: (28)" in str(exc) or "timed out" in str(exc).lower()
                if timed_out:
                    message = f"全文整理超过{settings.document_cleanup_timeout_seconds}秒总期限；原稿未改动，可重试。"
                elif isinstance(exc, ValueError) or any(mark in str(exc) for mark in ("SSE", "response.incomplete", "未完整结束", "未找到 output_text")):
                    message = "全文整理返回内容不完整或格式无效；原稿未改动，请重试。"
                else:
                    message = "全文整理网关连接失败或返回错误；请检查网关设置后重试。原稿未改动。"
                state["document_cleanup_error"] = message
                self.commit(state, event="document.failed")
                raise CleanupFailed(message, timed_out=timed_out) from exc
            suggestion = {"id": uuid4().hex, "original": original,
                "text": result["text"], "needs_review": result["needs_review"],
                "metrics": result.get("metrics", {}), "base_content_revision": content_revision}
            self.storage.artifact(sid, "cleaned", "document-" + suggestion["id"], suggestion)
            if state.get("content_revision", 0) != content_revision:
                state["document_cleanup_error"] = "整理期间正文已修改，旧建议已留存记录；请以新正文重新整理。"
                self.commit(state, event="document.failed")
                raise Conflict("整理期间正文已修改，未应用迟到结果，请重新预览")
            state["document_suggestion"] = suggestion
            self.commit(state, event="document.suggested")
            return public_state(state)
        targets = [u for u in state["utterances"] if u.get("raw_text") and
                   ((request.scope == "failed" and u["status"] == "cleanup_failed") or
                    (request.scope == "utterance" and u["id"] == request.utterance_id))]
        if request.scope == "utterance" and not targets:
            raise KeyError("可整理的语句不存在")
        for u in targets:
            await self.clean_utterance(state, u["id"], u["revision"])
        return public_state(state)

    async def close(self):
        self.closing = True
        interrupted = [state for state in self.states.values() if state["status"] in ACTIVE]
        tasks = {task for group in self.background.values() for task in group}
        # Stop all producers before waiting: manual and runtime HTTP requests
        # share one shutdown window, rather than two sequential remote timeouts.
        for runtime in self.runtimes.values():
            runtime.cancelled = True
            if runtime.task and not runtime.task.done():
                tasks.add(runtime.task)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await self.recognition.close()
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        for state in interrupted:
            if state["id"] not in self.deleting:
                state.update(status="interrupted", phase="服务停止，已完成结果保留", error="服务已停止，录音未继续")
                self.commit(state)

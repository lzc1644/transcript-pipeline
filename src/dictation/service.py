"""Event-loop-owned session state; worker processes never write the draft."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import struct
import time
from uuid import uuid4
import wave

from src.config_loader import load_settings
from src.codex_lb_client import CodexLBClientError
from src.dictation.cleanup import Cleanup
from src.dictation.schemas import SessionRequest, load_dictation_settings
from src.dictation.storage import ACTIVE, Storage, atomic_json, public_state

RATE = 16000


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


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

    def __init__(self, manager, state, lock_file):
        self.manager, self.state, self.lock_file = manager, state, lock_file
        self.cpu = self.final = None
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
        self.attached = False
        self.ever_attached = False
        self.ready_at = None
        self.disconnected_at = None
        self.last_audio_at = time.monotonic()
        self.last_voice_sample = 0
        self.audio_clock = None
        self.packet_lock = asyncio.Lock()
        self.cleanup_tasks = []

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
            self.final = manager.worker_factory(cfg["final_python"], "final", config_path, manager.root,
                                                directory / "final.log", self.lock_file.fileno())
            state.update(status="preparing", phase="准备 CPU 预览和 GPU 最终识别模型")
            self.save()
            loading = [asyncio.create_task(self.cpu.start()), asyncio.create_task(self.final.start())]
            jobs.extend(loading)
            cpu_metadata, final_metadata = await asyncio.gather(*loading)
            state["metrics"].update(cpu_worker=cpu_metadata, final_worker=final_metadata)
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
            await self.final.close()
            self.release_gpu()
            await self.cleanup_queue.put(None)
            await asyncio.gather(*self.cleanup_tasks)
            if not self.cancelled:
                state.update(status="interrupted" if state.get("interrupted") else "completed",
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
            for worker in (self.cpu, self.final):
                if worker:
                    await worker.close()
            self.release_gpu()
            for u in state["utterances"]:
                if u["status"] in {"partial", "finalizing"}:
                    u.update(status="asr_failed", error="会话中断，最终识别未完成")
                elif u["status"] == "cleaning":
                    u.update(status="cleanup_failed", error="整理未完成，原始文字可用")
            if self.cancelled:
                state.update(status="cancelled", phase="已取消，已完成结果保留")
            self.save()
            self.changed.set()

    def release_gpu(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

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
                    u["first_partial_seconds"] = time.monotonic() - self.audio_clock - u["start_sample"] / RATE
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
                    u["endpoint_latency_seconds"] = u["endpoint_at"] - self.audio_clock - u["last_voice_sample"] / RATE
                self.save()
                self.emit("utterance.pending", utterance_id=u["id"], revision=u["revision"])
                await self.queue.put((u["id"], path, time.monotonic()))
            elif kind == "drained":
                self.cpu_drained = True
                await self.queue.put(None)
                return
            elif kind == "decoded":
                self.state["decoded"] = True
            elif kind == "error":
                raise RuntimeError(event["error"])

    async def final_loop(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            uid, path, queued = item
            u = self.utterance(uid)
            await self.final.send({"op": "transcribe", "utterance_id": uid, "path": str(path)})
            result = await asyncio.wait_for(self.final.read(), 60)
            if self.cancelled:
                return
            if result["type"] == "final":
                u.update(raw_text=result["text"], status="raw", asr_seconds=result["inference_seconds"])
                if "gpu_peak_allocated_bytes" in result:
                    self.state["metrics"].update(gpu_peak_allocated_bytes=result["gpu_peak_allocated_bytes"],
                                                 gpu_allocated_bytes=result["gpu_allocated_bytes"])
                if not u["edited"]:
                    u["text"] = result["text"]
                u["revision"] += 1
                if self.audio_clock is not None:
                    u["final_latency_seconds"] = time.monotonic() - self.audio_clock - u["last_voice_sample"] / RATE
                self.manager.storage.artifact(self.state["id"], "asr", uid, result)
                self.save()
                self.emit("asr.final", utterance_id=uid, text=result["text"], revision=u["revision"])
                if self.state["config"]["cleanup"]:
                    if self.cleanup_queue.full():
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
                if result.get("fatal"):
                    raise RuntimeError(result["error"])
            else:
                raise RuntimeError(result.get("error", "最终识别 worker 协议无效"))

    async def cleanup_loop(self):
        while True:
            item = await self.cleanup_queue.get()
            if item is None:
                return
            uid, revision = item
            await self.manager.clean_utterance(self.state, uid, revision, self)

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
            if state["status"] != "listening":
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
                await self.stop("处理积压超过5秒，已停止新增采音")
                raise Conflict("处理积压超过5秒；已接收音频将继续处理")
            if lag > 2:
                self.emit("session.warning", message="音频积压超过2秒")
            self.manager.storage.append_audio(state["id"], pcm)
            state["packets"][str(seq)] = [start, len(pcm) // 2]
            if len(state["packets"]) > 128:
                del state["packets"][next(iter(state["packets"]))]
            state["next_sequence"] += 1
            state["samples"] += len(pcm) // 2
            self.last_audio_at = time.monotonic()
            if self.audio_clock is None:
                self.audio_clock = self.last_audio_at - len(pcm) / 2 / RATE
            self.save()
            await self.send_cpu({"op": "audio", "pcm": base64.b64encode(pcm).decode()})
            return self.emit("audio.ack", sequence=seq, next_sequence=state["next_sequence"], samples=state["samples"])

    async def stop(self, reason="正在完成尾部识别和整理"):
        if self.state["status"] in {"created", "preparing"}:
            await self.manager.cancel(self.state["id"], self.state["owner"])
            return
        if self.state["status"] != "listening":
            return
        self.state.update(status="draining", phase=reason)
        self.draining_started_at = time.monotonic()
        self.save()
        self.emit("session.draining", message=reason)
        await self.send_cpu({"op": "stop"})

    def silence_seconds(self):
        # Acoustic activity belongs only to VAD, never delayed ASR text.
        return max(0, self.processed - self.last_voice_sample) / RATE

    def sample_resources(self):
        for name, worker in (("cpu", self.cpu), ("final", self.final)):
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
            if (self.state["status"] == "draining" and not self.cpu_drained
                    and self.draining_started_at is not None
                    and clock - self.draining_started_at >= self.CPU_DRAIN_TIMEOUT_SECONDS):
                self.failure = "停止后的 CPU 收尾超过期限，已接收音频保留"
                self.failure_signal.set()
                return
            if self.state["mode"] != "live" or self.state["status"] != "listening":
                continue
            idle = self.silence_seconds()
            cfg = self.state["worker_config"]
            if not self.ever_attached and self.state["samples"] == 0 and self.ready_at is not None:
                if clock - self.ready_at >= cfg["disconnect_seconds"]:
                    self.state["interrupted"] = True
                    await self.stop("未在期限内建立录音连接，已释放资源")
                    continue
            if idle >= cfg["idle_seconds"]:
                await self.stop("长时间无语音，自动停止监听")
            elif idle >= cfg["idle_seconds"] - 10:
                self.emit("session.warning", message="10秒内仍无语音将自动停止")
            if self.disconnected_at and clock - self.disconnected_at >= cfg["disconnect_seconds"]:
                await self.stop("网络中断超出恢复窗口，已停止监听")
                self.state["interrupted"] = True
            if clock - self.last_audio_at > 15 and self.attached:
                await self.stop("未收到麦克风音频，已停止监听")

    async def feed_upload(self):
        path = self.manager.storage.path(self.state["id"])
        await self.send_cpu({"op": "decode", "source": str(path / self.state["upload_name"]),
                             "destination": str(path / "audio/decoded.wav"),
                             "max_seconds": self.state["config"]["max_upload_minutes"] * 60})
        # Decode response is handled by cpu_loop; do not race a second reader.
        deadline = time.monotonic() + 120
        while not (path / "audio/decoded.wav").exists() or not self.state.get("decoded"):
            if time.monotonic() > deadline:
                raise RuntimeError("录音解码超时")
            await asyncio.sleep(0.05)
        with wave.open(str(path / "audio/decoded.wav")) as source:
            while pcm := source.readframes(1280):
                while self.state["samples"] - self.processed > RATE:
                    await asyncio.sleep(0.02)
                packet = struct.pack("<IQ", self.state["next_sequence"], self.state["samples"]) + pcm
                await self.accept_audio(packet)
        await self.stop()


class DictationService:
    UPLOAD_RECEIVE_TIMEOUT_SECONDS = 300

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
        self.cleanup_slots = asyncio.Semaphore(2)
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
        if event and runtime:
            data = {"utterance_id": uid}
            if uid:
                data["revision"] = runtime.utterance(uid)["revision"]
            runtime.emit(event, **data)
        if event and not runtime:
            state["event_sequence"] += 1
            self.storage.record(state["id"], {"type": event, "session_id": state["id"],
                                "utterance_id": uid, "revision": state["revision"],
                                "event_sequence": state["event_sequence"]})
        self.storage.save(state)

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
            if any(r.task and not r.task.done() for r in self.runtimes.values()):
                raise Conflict("已有语音输入会话正在处理，请先停止并等待完成")
            loaded = load_settings(project_root=self.root)
            config = settings.freeze(self.root, loaded, request, preview=not upload_name)
            for key in ("cpu_python", "final_python", "vad_model"):
                if not Path(config[key]).is_file():
                    raise Conflict(f"语音输入尚未准备好：{key} 不存在，请查看 docs/DICTATION.md")
            if not upload_name and not (Path(config["streaming_model"]) / "encoder.int8.onnx").is_file():
                raise Conflict("流式模型尚未预下载，请运行 scripts/setup_dictation.py")
            lock_path = self.root / "data/jobs/_asr-gpu.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise Conflict("GPU 被其他 ASR 任务占用，请等待任务完成；不会抢占") from None
            sid = uuid4().hex
            state = {"id": sid, "owner": owner, "status": "created", "phase": "等待准备模型",
                     "mode": "upload" if upload_name else "live", "created_at": now(), "updated_at": now(),
                     "revision": 0, "event_sequence": 0, "samples": 0, "next_sequence": 0, "packets": {},
                     "utterances": [], "metrics": {}, "worker_config": config,
                     "config": {k: config[k] for k in ("terms", "cleanup", "cleanup_model", "reasoning_effort",
                         "document_cleanup_model", "document_reasoning_effort",
                         "streaming_model_id", "streaming_revision", "qwen_model", "qwen_revision",
                         "max_upload_mb", "max_upload_minutes", "segmentation")}}
            if upload_name:
                state["upload_name"] = upload_name
            from src.web.frontend_settings import load_frontend_settings
            frontend = load_frontend_settings(self.root)
            gateway = loaded.settings.codex_lb
            state["gateway"] = gateway.model_copy(update={
                "base_url": os.getenv(gateway.base_url_env, "") or frontend.codex_lb_base_url or gateway.base_url,
                "base_url_env": "",
            }).model_dump()
            try:
                self.storage.create(state)
                self.states[sid] = state
                self.projections[sid] = ""
                runtime = Runtime(self, state, lock)
                self.runtimes[sid] = runtime
                if start:
                    runtime.task = asyncio.create_task(runtime.run())
            except BaseException:
                lock.close()
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
            # The runtime identity is the lifecycle token. Validation and start
            # are one event-loop turn, so deletion cannot slip between them.
            if (self.runtimes.get(sid) is not runtime or runtime.cancelled
                    or sid in self.deleting or self.closing or runtime.lock_file is None):
                raise Conflict("上传会话已取消或正在删除")
            state["upload_bytes"] = count
            self.commit(state)
            runtime.task = asyncio.create_task(runtime.run())
        except BaseException as exc:
            runtime.cancelled = True
            runtime.release_gpu()
            state.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                         phase="上传已取消" if isinstance(exc, asyncio.CancelledError) else "上传失败",
                         error=("上传未完成" if isinstance(exc, asyncio.CancelledError) else
                                "上传接收超过期限，请重新上传" if isinstance(exc, TimeoutError) else str(exc)))
            self.commit(state)
            raise
        finally:
            self.background[sid].discard(task)
        return state

    def gateway(self, state):
        from src.schemas import CodexLBSettings
        return CodexLBSettings.model_validate(state["gateway"])

    def cleaner(self, state):
        cfg = state["worker_config"]
        kwargs = {"project_root": self.root,
                  "document_model": cfg.get("document_cleanup_model", "gpt-6-luna"),
                  "document_timeout": cfg.get("document_cleanup_timeout_seconds", 120)} if self.cleanup_factory is Cleanup else {}
        return self.cleanup_factory(self.gateway(state), cfg["cleanup_model"], cfg["cleanup_timeout_seconds"], **kwargs)

    async def external(self, function, *args):
        async with self.cleanup_slots:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep the slot until the bounded transport actually exits.
                await asyncio.gather(task, return_exceptions=True)
                raise

    async def clean_utterance(self, state, uid, revision, runtime=None):
        u = next(u for u in state["utterances"] if u["id"] == uid)
        context = [v["text"] for v in state["utterances"][:state["utterances"].index(u)] if v.get("raw_text")][-2:]
        cfg = state["worker_config"]
        try:
            cleaner = self.cleaner(state)
            result = await self.external(cleaner, u["raw_text"], context, cfg["terms"])
            if runtime and runtime.cancelled:
                return
            self.storage.artifact(state["id"], "cleaned", uid, result)
            u.update(cleaned_text=result["text"],
                     needs_review=result["needs_review"] or u.get("boundary") == "hard_limit", status="cleaned")
            u.pop("error", None)
            if not u["edited"] and u["revision"] == revision:
                u["text"] = result["text"]
                u.pop("suggestion", None)
            else:
                u["suggestion"] = result["text"]
            u["revision"] += 1
            if runtime and runtime.audio_clock is not None:
                u["cleanup_latency_seconds"] = time.monotonic() - runtime.audio_clock - u["last_voice_sample"] / RATE
            self.commit(state, event="cleanup.completed", uid=uid)
        except Exception as exc:
            u.update(status="cleanup_failed", error=str(exc), revision=u["revision"] + 1)
            self.commit(state, event="utterance.failed", uid=uid)

    def edit(self, sid, owner, uid, revision, text, discard_document=False):
        state = self.get(sid, owner)
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
        if state["revision"] != revision:
            raise Conflict("正文已更新；你的编辑未被覆盖，请刷新后重试")
        if state["status"] in ACTIVE:
            raise Conflict("整篇编辑需等待停止后的处理完成")
        state.update(document_text=text, document_revision=revision + 1)
        self.commit(state, event="document.edited")
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
                runtime.release_gpu()
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
        if sum(len(group) for group in self.background.values()) >= 2:
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
            suggestion = state.get("document_suggestion")
            if (not suggestion or suggestion["id"] != request.suggestion_id or request.revision != state["revision"]
                    or suggestion["base_content_revision"] != state.get("content_revision", 0)):
                raise Conflict("正文或建议版本已变化，请重新预览差异")
            state["document_text"] = suggestion["text"]
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
            try:
                cleaner = self.cleaner(state)
                result = await self.external(cleaner.document, original, [], cfg["terms"])
            except (CodexLBClientError, ValueError, TimeoutError) as exc:
                timed_out = isinstance(exc, TimeoutError) or "curl: (28)" in str(exc) or "timed out" in str(exc).lower()
                if timed_out:
                    message = f"全文整理超过{cfg.get('document_cleanup_timeout_seconds', 120)}秒总期限；原稿未改动，可重试。"
                elif isinstance(exc, ValueError) or any(mark in str(exc) for mark in ("SSE", "response.incomplete", "未完整结束", "未找到 output_text")):
                    message = "全文整理返回内容不完整或格式无效；原稿未改动，请重试。"
                else:
                    message = "全文整理网关连接失败或返回错误；请检查网关设置后重试。原稿未改动。"
                state["document_cleanup_error"] = message
                self.commit(state, event="document.failed")
                raise CleanupFailed(message, timed_out=timed_out) from exc
            if state.get("content_revision", 0) != content_revision:
                raise Conflict("整理期间正文已修改，未应用迟到结果，请重新预览")
            state["document_suggestion"] = {"id": uuid4().hex, "original": original,
                "text": result["text"], "needs_review": result["needs_review"],
                "metrics": result.get("metrics", {}), "base_content_revision": content_revision}
            self.storage.artifact(sid, "cleaned", "document-" + state["document_suggestion"]["id"], state["document_suggestion"])
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
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        for runtime in self.runtimes.values():
            if runtime.lock_file:
                runtime.release_gpu()
        for state in interrupted:
            if state["id"] not in self.deleting:
                state.update(status="interrupted", phase="服务停止，已完成结果保留", error="服务已停止，录音未继续")
                self.commit(state)

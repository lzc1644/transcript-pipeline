"""One Qwen process and output reader, with one turn per ready session."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import fcntl
import time
from uuid import uuid4

from src.dictation.storage import atomic_json


class Conflict(ValueError):
    pass


class RecognitionBusy(Conflict):
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


@dataclass
class Request:
    runtime: object
    command: dict
    queued_at: float
    future: asyncio.Future


class RecognitionScheduler:
    INFERENCE_TIMEOUT_SECONDS = 60

    def __init__(self, service):
        self.service = service
        self.members = {}
        self.pending = deque()
        self.active = None
        self.last_session = None
        self.worker = None
        self.lock_file = None
        self.loading = None
        self.runner = None
        self.failure = None
        self.lifecycle = asyncio.Lock()
        self.wake = asyncio.Event()

    async def reserve(self, runtime, maximum):
        async with self.lifecycle:
            sid = runtime.state["id"]
            if runtime.cancelled or self.service.closing or sid in self.service.deleting:
                raise Conflict("会话已取消或服务正在停止")
            if sid in self.members:
                return
            if self.failure:
                raise RecognitionBusy("共享识别进程正在回收，请稍后重试", "recovering")
            if len(self.members) >= maximum:
                raise RecognitionBusy("当前语音识别容量已满，请稍后重试", "capacity")
            if self.lock_file is None:
                path = self.service.root / "data/jobs/_asr-gpu.lock"
                path.parent.mkdir(parents=True, exist_ok=True)
                lock = path.open("a")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock.close()
                    raise RecognitionBusy("GPU 正被批量转录占用，请等待完成", "batch_asr") from None
                self.lock_file = lock
            self.members[sid] = runtime
            runtime.asr_reserved = True

    async def start(self, runtime):
        # reserve precedes run; all callers await the same loader without owning it.
        if runtime.failure or not runtime.asr_reserved:
            raise RuntimeError(runtime.failure or "会话识别资源已释放")
        if self.loading is None:
            directory = self.service.storage.root / "_recognition"
            directory.mkdir(exist_ok=True)
            config = directory / "worker.json"
            cfg = runtime.state["worker_config"]
            atomic_json(config, {key: cfg[key] for key in ("qwen_cache", "qwen_model", "qwen_revision")})
            self.worker = self.service.worker_factory(cfg["final_python"], "final", config,
                self.service.root, directory / "final.log", self.lock_file.fileno())
            self.loading = asyncio.create_task(self._load())
        return await asyncio.shield(self.loading)

    async def _load(self):
        try:
            metadata = await self.worker.start()
            self.runner = asyncio.create_task(self._run())
            return metadata
        except Exception as exc:
            await self._fail(str(exc))
            raise

    async def transcribe(self, runtime, uid, path, queued_at):
        if runtime.failure or not runtime.asr_reserved:
            raise RuntimeError(runtime.failure or "会话识别资源已释放")
        command = {"op": "transcribe", "session_id": runtime.state["id"], "utterance_id": uid,
                   "request_id": uuid4().hex, "path": str(path),
                   "terms": list(runtime.state["worker_config"]["terms"])}
        future = asyncio.get_running_loop().create_future()
        request = Request(runtime, command, queued_at, future)
        self.pending.append(request)
        runtime.utterance(uid)["asr_queue_state"] = "queued"
        runtime.save()
        runtime.emit("asr.queued", utterance_id=uid)
        self.wake.set()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if request in self.pending:
                self.pending.remove(request)
                future.cancel()
            else:
                # A shared process cannot be killed to cancel one user's inference.
                await asyncio.shield(asyncio.gather(future, return_exceptions=True))
            raise

    def _next(self):
        eligible = [r for r in self.pending if not r.runtime.cancelled and
                    (r.runtime.state["mode"] != "upload" or r.runtime.dispatch_open.is_set())]
        if not eligible:
            return None
        request = next((r for r in eligible if r.command["session_id"] != self.last_session), eligible[0])
        self.pending.remove(request)
        self.last_session = request.command["session_id"]
        return request

    async def _run(self):
        # This is the sole output reader even while no session has queued speech.
        reading = asyncio.create_task(self.worker.read())
        try:
            while True:
                self.wake.clear()
                if reading.done():
                    result = reading.result()
                    raise RuntimeError(result.get("error", "共享识别 worker 返回了未请求的结果"))
                request = self._next()
                if request is None:
                    waking = asyncio.create_task(self.wake.wait())
                    try:
                        await asyncio.wait([waking, reading], return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        waking.cancel()
                        await asyncio.gather(waking, return_exceptions=True)
                    continue
                self.active = request
                runtime, command = request.runtime, request.command
                runtime.try_begin_dispatch("asr")  # No await between gate check and dispatch registration.
                u = runtime.utterance(command["utterance_id"])
                u.update(asr_queue_state="running", queue_wait_seconds=time.monotonic() - request.queued_at)
                runtime.save()
                runtime.emit("asr.started", utterance_id=u["id"])
                # Queue wait is deliberately outside this inference/IPC deadline.
                async with asyncio.timeout(self.INFERENCE_TIMEOUT_SECONDS):
                    await self.worker.send(command)
                    result = await asyncio.shield(reading)
                if any(result.get(key) != command[key] for key in ("session_id", "utterance_id", "request_id")):
                    raise RuntimeError("共享识别 worker 任务标识不匹配")
                if result["type"] not in {"final", "failed"} or result.get("fatal"):
                    raise RuntimeError(result.get("error", "共享识别 worker 协议无效"))
                result["queue_wait_seconds"] = u["queue_wait_seconds"]
                self.active = None
                request.future.set_result(result)
                reading = asyncio.create_task(self.worker.read())
        except Exception as exc:
            message = "共享识别推理超过60秒，已停止接收新音频" if isinstance(exc, TimeoutError) else str(exc)
            await self._fail(message)
        finally:
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

    async def _fail(self, message):
        self.failure = message
        for runtime in self.members.values():
            runtime.failure = message
            runtime.failure_signal.set()
        async with self.lifecycle:
            await self._shutdown(message)

    async def _shutdown(self, message):
        current = asyncio.current_task()
        tasks = [task for task in (self.loading, self.runner) if task and task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.worker:
            await self.worker.close()  # Actual process exit must precede flock release.
        if self.lock_file:
            self.lock_file.close()
        for request in [*self.pending, *([self.active] if self.active else [])]:
            if not request.future.done():
                request.future.set_exception(RuntimeError(message))
        self.pending.clear()
        self.active = None
        for runtime in self.members.values():
            runtime.asr_reserved = False
        self.members.clear()
        self.worker = self.lock_file = self.loading = self.runner = None
        self.last_session = self.failure = None

    async def release(self, runtime):
        async with self.lifecycle:
            if self.members.get(runtime.state["id"]) is not runtime:
                return
            del self.members[runtime.state["id"]]
            runtime.asr_reserved = False
            if not self.members:
                await self._shutdown("识别会话已结束")

    async def close(self):
        async with self.lifecycle:
            await self._shutdown("服务正在停止，已完成结果保留")

"""One sample-clock segmenter, shared by realtime and uploaded audio."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

RATE = 16000
FRAME = 512


class SileroVAD:
    """Incremental state for the project's faster-whisper Silero v6 ONNX asset."""

    def __init__(self, path: Path):
        import numpy as np
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        import numpy as np
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = self.h.copy()
        self.context = np.zeros((1, 64), dtype=np.float32)

    def __call__(self, frame):
        import numpy as np
        batch = np.concatenate((self.context, frame.reshape(1, -1)), axis=1)
        out, self.h, self.c = self.session.run(None, {"input": batch, "h": self.h, "c": self.c})
        self.context = frame[-64:].reshape(1, -1).copy()
        return float(out.reshape(-1)[0])


@dataclass(frozen=True)
class SegmentConfig:
    pre_ms: int = 250
    silence_ms: int = 700
    tail_ms: int = 150
    target_seconds: float = 20
    hard_seconds: float = 25
    threshold: float = 0.5


class Segmenter:
    """Emits start/audio/end in order; keeps at most one short pre-roll."""

    def __init__(self, vad, config=SegmentConfig()):
        self.vad, self.config = vad, config
        self.position = 0
        self.start = None
        self.last_voice = 0
        self.last_activity = 0
        self.pre = b""
        self.pending = b""
        self.continuation = False

    def pause(self):
        events = self.feed(b"", finish=True, boundary="pause")
        # Preserve the sample clock, not acoustic context across a mic gap.
        self.pre = self.pending = b""
        self.continuation = False
        self.last_voice = self.last_activity = self.position
        reset = getattr(self.vad, "reset", None)
        if reset:
            reset()
        return events

    def feed(self, pcm: bytes, *, finish=False, boundary="stop"):
        import numpy as np
        self.pending += pcm
        events = []
        while len(self.pending) >= FRAME * 2 or (finish and self.pending):
            length = min(FRAME * 2, len(self.pending))
            if self.start is not None:
                remaining = round(self.config.hard_seconds * RATE) - (self.position - self.start)
                length = min(length, remaining * 2)
            raw, self.pending = self.pending[:length], self.pending[length:]
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            frame = np.pad(samples, (0, FRAME - len(samples)))
            voiced = self.vad(frame) >= self.config.threshold
            begin, self.position = self.position, self.position + len(samples)
            if voiced:
                self.last_voice = self.last_activity = self.position
            if self.start is None and (voiced or self.continuation):
                self.start = begin - len(self.pre) // 2
                events.append({"type": "start", "start": self.start})
                if self.pre:
                    events.append({"type": "audio", "pcm": self.pre})
                self.pre = b""
                self.continuation = False
            if self.start is not None:
                events.append({"type": "audio", "pcm": raw})
                duration = self.position - self.start
                silence = self.position - self.last_voice
                hard = duration >= round(self.config.hard_seconds * RATE)
                soft = duration >= round(self.config.target_seconds * RATE) and silence >= RATE // 5
                if hard or soft or silence >= self.config.silence_ms * RATE // 1000:
                    end = self.position if hard else min(self.position, self.last_voice + self.config.tail_ms * RATE // 1000)
                    events.append({"type": "end", "start": self.start, "end": end,
                                   "last_voice": min(end, self.last_voice),
                                   "boundary": "hard_limit" if hard else "weak_pause" if soft else "silence"})
                    self.start = None
                    self.continuation = hard and voiced
            else:
                size = self.config.pre_ms * RATE // 1000 * 2
                self.pre = (self.pre + raw)[-size:] if size else b""
        if finish and self.start is not None:
            events.append({"type": "end", "start": self.start, "end": self.position,
                           "last_voice": min(self.position, self.last_voice), "boundary": boundary})
            self.start = None
        return events


def decode_file(source: Path, destination: Path, max_seconds: int):
    """Bounded streaming decode; never trusts container-reported duration alone."""
    import av
    import numpy as np
    import wave
    total = 0
    with av.open(str(source), options={"protocol_whitelist": "file,pipe"}) as container, wave.open(str(destination), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("文件不包含音轨")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                raw = converted.to_ndarray().astype("<i2").tobytes()
                total += len(raw) // 2
                if total > max_seconds * RATE:
                    raise ValueError(f"录音超过 {max_seconds // 60} 分钟上限")
                output.writeframesraw(raw)
        for converted in resampler.resample(None):
            raw = converted.to_ndarray().astype("<i2").tobytes()
            total += len(raw) // 2
            if total > max_seconds * RATE:
                raise ValueError("录音时长超限")
            output.writeframesraw(raw)
    if not total:
        raise ValueError("空音频或损坏文件")
    return total

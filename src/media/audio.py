"""Bounded audio decoding for uploads, without session state."""
from __future__ import annotations

from pathlib import Path

RATE = 16000


def decode_audio_file(source: Path, destination: Path, max_seconds: int):
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

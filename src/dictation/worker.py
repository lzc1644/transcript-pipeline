"""Line-framed private IPC. Invoked only with a project-owned isolated Python."""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import time

OUTPUT = sys.stdout


def emit(event):
    OUTPUT.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    OUTPUT.flush()


def cpu_worker(config):
    import numpy as np
    from src.asr.streaming_audio import Segmenter, SegmentConfig, SileroVAD
    from src.asr.streaming import PREVIEW_THREADS, StreamingASR
    segmenter = Segmenter(SileroVAD(Path(config["vad_model"])), SegmentConfig(**config["segmentation"]))
    asr = StreamingASR(Path(config["streaming_model"])) if config.get("preview", True) else None
    utterance = 0
    last_text = ""
    last_update = 0
    import importlib.metadata
    emit({"type": "ready", "metadata": {"sherpa_onnx": importlib.metadata.version("sherpa-onnx"),
          "onnxruntime": importlib.metadata.version("onnxruntime"), "preview_enabled": asr is not None,
          "preview_threads": PREVIEW_THREADS if asr is not None else None}})
    for line in sys.stdin:
        command = json.loads(line)
        operation = command["op"]
        if operation == "decode":
            from src.media.audio import decode_audio_file as decode_file
            count = decode_file(Path(command["source"]), Path(command["destination"]), command["max_seconds"])
            emit({"type": "decoded", "samples": count})
            continue
        if operation == "cancel":
            return
        finishing = operation == "stop"
        raw = base64.b64decode(command.get("pcm", ""), validate=True)
        events = segmenter.pause() if operation == "pause" else segmenter.feed(raw, finish=finishing)
        for event in events:
            if event["type"] == "start":
                utterance += 1
                last_text = ""
                if asr:
                    asr.reset()
                emit({"type": "utterance.start", "utterance_id": str(utterance), "start": event["start"]})
            elif event["type"] == "audio" and asr:
                text = asr.feed(np.frombuffer(event["pcm"], dtype="<i2").astype(np.float32) / 32768)
                now = time.monotonic()
                if text != last_text and now - last_update >= 0.1:
                    emit({"type": "partial", "utterance_id": str(utterance), "text": text})
                    last_text, last_update = text, now
            elif event["type"] == "end":
                text = asr.feed(np.zeros(0, dtype=np.float32), finish=True) if asr else ""
                emit({**event, "type": "utterance.end", "utterance_id": str(utterance), "preview": text})
        if finishing:
            emit({"type": "drained"})
            return
        emit({"type": "processed", "samples": segmenter.position,
              "last_voice_sample": segmenter.last_activity})
        if operation == "pause":
            if asr:
                asr.reset()
            last_text, last_update = "", 0
            emit({"type": "paused", "samples": segmenter.position})


def final_worker(config):
    import torch
    torch.set_num_threads(4)
    from src.asr.utterance import UtteranceASR
    torch.cuda.reset_peak_memory_stats()
    asr = UtteranceASR(Path(config["qwen_cache"]))
    emit({"type": "ready", "metadata": {"aligner_loaded": asr.backend.model.forced_aligner is not None,
          "torch_cuda": torch.version.cuda, "torch": torch.__version__}})
    for line in sys.stdin:
        command = json.loads(line)
        if command["op"] in {"stop", "cancel"}:
            return
        started = time.monotonic()
        identity = {key: command[key] for key in ("session_id", "utterance_id", "request_id")}
        try:
            text = asr.transcribe(Path(command["path"]), command["terms"])
            emit({"type": "final", **identity, "text": text,
                  "inference_seconds": time.monotonic() - started,
                  "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                  "gpu_allocated_bytes": torch.cuda.memory_allocated()})
        except Exception as exc:
            emit({"type": "failed", **identity, "error": str(exc),
                  "fatal": "out of memory" in str(exc).lower()})
            if "out of memory" in str(exc).lower():
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["cpu", "final"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        with redirect_stdout(sys.stderr):
            (cpu_worker if args.mode == "cpu" else final_worker)(config)
    except Exception as exc:
        emit({"type": "error", "error": str(exc)})
        raise


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Replay the same WAVs through the actual dictation workers (not a benchmark platform)."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import struct
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.dictation.schemas import SessionRequest
from src.dictation.service import Conflict, DictationService
from src.dictation.storage import public_state


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)


async def replay(args):
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(args.manifest.read_text())
    # A probe must never reconcile another running Web server's sessions.
    service = DictationService(args.project_root.resolve() if args.project_root else ROOT, recover=False)
    reports = []
    try:
        for item in manifest:
            source = Path(item["path"])
            if not source.is_absolute():
                source = ROOT / source
            state = await service.create("local-validation", SessionRequest(cleanup=args.cleanup),
                                         upload_name="upload.wav" if args.candidate == "final" else None,
                                         start=args.candidate != "final")
            runtime = service.runtimes[state["id"]]
            if args.candidate == "final":
                shutil.copyfile(source, service.storage.path(state["id"]) / "upload.wav")
                runtime.task = asyncio.create_task(runtime.run())
            deadline = time.monotonic() + 120
            while state["status"] in {"created", "preparing"}:
                if time.monotonic() > deadline:
                    raise RuntimeError("模型准备超时")
                await asyncio.sleep(.05)
            if state["status"] != "listening":
                raise RuntimeError(state.get("error", "模型准备失败"))
            if args.candidate != "final":
                with wave.open(str(source)) as audio:
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                        raise ValueError("回放输入必须为16k单声道PCM16 WAV")
                    started = time.monotonic()
                    while pcm := audio.readframes(1280):
                        if args.realtime:
                            await asyncio.sleep(max(0, started + (state["samples"] + len(pcm) // 2) / 16000 - time.monotonic()))
                        else:
                            while state["samples"] - runtime.processed > 16000:
                                await asyncio.sleep(.01)
                        packet = struct.pack("<IQ", state["next_sequence"], state["samples"]) + pcm
                        try:
                            await runtime.accept_audio(packet)
                        except Conflict:
                            if state["status"] != "draining":
                                raise
                            break  # E.g. automatic idle stop; report actual received coverage.
                await runtime.stop()
            await asyncio.wait_for(asyncio.shield(runtime.task), 180)
            report = {"sample_id": item.get("id"), "recording_group": item.get("recording_group"),
                      "split": item.get("split"), "categories": item.get("categories", []),
                      "source": str(source), "candidate": args.candidate, "realtime": args.realtime,
                      "reference": item.get("reference"), "session_directory": str(service.storage.path(state["id"])),
                      "received_audio_seconds": state["samples"] / 16000,
                      "result": public_state(state)}
            reports.append(report)
            (args.output / f"{len(reports):03d}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        utterances = [u for r in reports for u in r["result"]["utterances"]]
        summary = {"samples": len(reports), "utterances": len(utterances), "realtime": args.realtime,
                   "status": [r["result"]["status"] for r in reports], "cleanup_enabled": args.cleanup}
        for key in ("first_partial_seconds", "final_latency_seconds", "cleanup_latency_seconds", "asr_seconds"):
            values = [u[key] for u in utterances if key in u]
            summary[key] = {"count": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
        summary["manual_review"] = "Inspect source audio, reference and separate raw/cleaned text; no automatic quality pass."
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, help="existing isolated project root with code/models and shared GPU lock; no session recovery")
    parser.add_argument("--manifest", required=True, type=Path, help="JSON array: [{path, reference?}]")
    parser.add_argument("--candidate", choices=["dual", "final"], default="dual")
    parser.add_argument("--realtime", action="store_true", help="1x audio sample-clock replay, required for realtime latency claims")
    parser.add_argument("--cleanup", action="store_true", help="explicitly enable paid remote cleanup")
    parser.add_argument("--output", required=True, type=Path, help="new independent output directory; never overwritten")
    args = parser.parse_args()
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()

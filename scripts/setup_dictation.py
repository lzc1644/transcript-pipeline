#!/usr/bin/env python
"""Explicit, offline-at-session-start CPU model preparation."""
from __future__ import annotations

import argparse
import importlib.resources
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.dictation.streaming_asr import MODEL_ID, MODEL_REVISION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="create and install the isolated CPU environment")
    parser.add_argument("--download", action="store_true", help="download the pinned INT8 model and example WAVs")
    args = parser.parse_args()
    interpreter = ROOT / ".venv/dictation-py312/bin/python"
    if args.install:
        uv = shutil.which("uv")
        if uv:
            if not interpreter.exists():
                subprocess.run([uv, "venv", "--python", sys.executable, str(interpreter.parents[1])], check=True)
            subprocess.run([uv, "pip", "install", "--python", str(interpreter), "-r", str(ROOT / "requirements-dictation.txt")], check=True)
        else:
            if not interpreter.exists():
                subprocess.run([sys.executable, "-m", "venv", str(interpreter.parents[1])], check=True)
            subprocess.run([str(interpreter), "-m", "pip", "install", "-r", str(ROOT / "requirements-dictation.txt")], check=True)
    if args.download:
        destination = ROOT / "data/models/dictation"
        destination.mkdir(parents=True, exist_ok=True)
        asset = importlib.resources.files("faster_whisper").joinpath("assets/silero_vad_v6.onnx")
        shutil.copyfile(str(asset), destination / "silero_vad_v6.onnx")
        subprocess.run([str(interpreter), "-c", "from huggingface_hub import snapshot_download; "
            f"snapshot_download({MODEL_ID!r}, revision={MODEL_REVISION!r}, "
            "allow_patterns=['*.int8.onnx','tokens.txt','test_wavs/*.wav'], "
            f"local_dir={str(destination / 'paraformer')!r})"], check=True)
        (destination / "model.json").write_text(json.dumps({"model_id": MODEL_ID, "revision": MODEL_REVISION,
             "vad": "faster-whisper packaged silero_vad_v6.onnx"}, indent=2))
    if not args.install and not args.download:
        parser.error("选择 --install 和/或 --download；不会在开始录音时自动安装/下载")


if __name__ == "__main__":
    main()

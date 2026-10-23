#!/usr/bin/env python
"""Serve current code against NEW test data, reusing offline models and the real GPU lock."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="new isolated test directory")
    parser.add_argument("--port", type=int, default=8023)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "config").mkdir()
    shutil.copyfile(ROOT / "config/settings.yaml", root / "config/settings.yaml")
    (root / "config/dictation.json").write_text(json.dumps({
        "cpu_python": str(ROOT / ".venv/dictation-py312/bin/python"),
        "final_python": str(ROOT / ".venv/asr-py312/bin/python"),
        "model_dir": str(ROOT / "data/models/dictation"),
    }))
    (root / "src").symlink_to(ROOT / "src", target_is_directory=True)
    (root / "frontend").symlink_to(ROOT / "frontend", target_is_directory=True)
    (root / "data/jobs").mkdir(parents=True)
    # Isolated sessions must still respect the GPU mutex used by production.
    lock = ROOT / "data/jobs/_asr-gpu.lock"
    if not lock.is_file():
        raise SystemExit("existing project GPU lock is required; do not create a separate GPU mutex")
    (root / "data/jobs/_asr-gpu.lock").symlink_to(lock)
    import uvicorn
    from api_server import create_app
    uvicorn.run(create_app(project_root=root), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()

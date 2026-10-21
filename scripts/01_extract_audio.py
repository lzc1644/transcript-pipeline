from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import ConfigLoadError, load_settings
from src.ffmpeg_utils import AudioExtractionError, extract_audio_batch, summarize_extraction_results
from src.runtime_utils import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将视频或音频文件统一转换为 WAV 音频。")
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        loaded_settings = load_settings(
            settings_path=args.config,
            profile_name=args.profile,
            project_root=PROJECT_ROOT,
        )
    except ConfigLoadError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(loaded_settings.settings.runtime.log_level)
    logger.info("阶段启动 | extract-audio | profile=%s", loaded_settings.active_profile_name)

    try:
        results = extract_audio_batch(loaded_settings, logger=logger)
    except AudioExtractionError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("阶段完成 | extract-audio | %s", summarize_extraction_results(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

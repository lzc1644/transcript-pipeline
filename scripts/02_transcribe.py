from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.asr_utils import AsrTranscriptionError, summarize_transcription_results, transcribe_batch
from src.config_loader import ConfigLoadError, load_settings
from src.runtime_utils import setup_logging
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用所选 ASR 候选转录音频文件。")
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    parser.add_argument("--asr-candidate", help="ASR 候选 ID；省略时使用任务配置或旧 Whisper 行为")
    parser.add_argument("--secondary-asr-candidate", help="第二 ASR 候选；空字符串关闭，省略继承配置")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        loaded_settings = load_settings(
            settings_path=args.config,
            profile_name=args.profile,
            project_root=PROJECT_ROOT,
        )
        apply_model_overrides(loaded_settings, ModelOverrides(asr_candidate=args.asr_candidate,
                              secondary_asr_candidate=args.secondary_asr_candidate))
    except (ConfigLoadError, SettingsOverrideError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(loaded_settings.settings.runtime.log_level)
    logger.info("阶段启动 | transcribe | profile=%s", loaded_settings.active_profile_name)

    try:
        outputs = transcribe_batch(loaded_settings, logger=logger)
    except AsrTranscriptionError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("阶段完成 | transcribe | %s", summarize_transcription_results(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

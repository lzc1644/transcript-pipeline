from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_pipeline import run_stage
from src.config_loader import ConfigLoadError, load_settings
from src.runtime_utils import normalize_stage_name, setup_logging
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按当前推荐主链顺序运行完整流水线。")
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    parser.add_argument("--asr-candidate", help="ASR 候选 ID")
    parser.add_argument("--secondary-asr-candidate", help="第二 ASR 候选；空字符串关闭双 ASR")
    parser.add_argument("--ocr-model", help="覆盖 Codex API OCR 使用的模型")
    parser.add_argument("--ocr-reasoning-effort", help="覆盖 Codex API OCR reasoning effort")
    parser.add_argument("--ocr-max-concurrency", type=int, help="覆盖 PDF OCR 最大在途请求数")
    parser.add_argument("--ocr-submit-interval-seconds", type=float, help="覆盖 PDF OCR 页面投递间隔秒数")
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
    try:
        apply_model_overrides(
            loaded_settings,
            ModelOverrides(
                asr_candidate=args.asr_candidate,
                secondary_asr_candidate=args.secondary_asr_candidate,
                ocr_model=args.ocr_model,
                ocr_reasoning_effort=args.ocr_reasoning_effort,
                ocr_max_concurrency=args.ocr_max_concurrency,
                ocr_submit_interval_seconds=args.ocr_submit_interval_seconds,
            ),
        )
    except SettingsOverrideError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(loaded_settings.settings.runtime.log_level)
    pipeline_settings = loaded_settings.settings.pipeline
    raw_stages = pipeline_settings.stages if pipeline_settings is not None else []
    if not raw_stages:
        logger.error("当前配置未定义主链阶段列表。")
        return 1

    stages = [normalize_stage_name(stage_name) for stage_name in raw_stages]
    logger.info("主链启动 | profile=%s | stages=%s", loaded_settings.active_profile_name, ",".join(stages))

    for stage_name in stages:
        logger.info("主链执行 | stage=%s", stage_name)
        exit_code = run_stage(stage_name, loaded_settings, logger)
        if exit_code != 0:
            logger.error("主链失败 | stage=%s | exit_code=%s", stage_name, exit_code)
            return exit_code

    logger.info("主链完成 | stages=%s", ",".join(stages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

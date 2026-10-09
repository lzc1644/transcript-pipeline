from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import ConfigLoadError, load_settings
from src.refine_utils import RefinementError, refine_batch, resolve_requested_backends, summarize_refinement_results
from src.runtime_utils import setup_logging
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行阶段 6：LLM 校对精修。")
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    parser.add_argument("--backend", choices=["codex_api", "agy", "codex_cli", "both"], help="覆盖阶段 6 使用的后端")
    parser.add_argument("--asr-candidate", help="主 ASR 身份，应与已有产物一致")
    parser.add_argument("--secondary-asr-candidate", help="第二 ASR 身份，要求有效完成配对；空字符串关闭")
    parser.add_argument("--model", help="覆盖阶段 6 使用的模型，例如 gpt-6.1-sol")
    parser.add_argument("--reasoning-effort", help="覆盖阶段 6 reasoning effort，例如 low / medium / high / xhigh / max")
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
            ModelOverrides(llm_model=args.model, llm_reasoning_effort=args.reasoning_effort,
                           asr_candidate=args.asr_candidate, secondary_asr_candidate=args.secondary_asr_candidate),
        )
    except SettingsOverrideError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(loaded_settings.settings.runtime.log_level)
    logger.info("阶段启动 | refine | profile=%s", loaded_settings.active_profile_name)

    try:
        requested_backends = resolve_requested_backends(args.backend, loaded_settings.settings.llm.backends)
        summary = refine_batch(loaded_settings, requested_backends=requested_backends, logger=logger)
    except RefinementError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("阶段完成 | refine | %s", summarize_refinement_results(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

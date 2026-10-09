from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import ConfigLoadError, load_settings
from src.job_runner import JobRunnerError, run_single_job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按单任务输入绑定方式运行完整主链。")
    parser.add_argument("--video", required=True, help="输入视频文件路径")
    parser.add_argument("--reference", help="参考源：本地 txt/md/pdf 或网页链接；对谈模式不需要")
    parser.add_argument("--output-dir", required=True, help="最终 Markdown 输出目录")
    parser.add_argument(
        "--content-type",
        choices=["book_club", "conversation"],
        default="book_club",
        help="内容类型：book_club 为读书会，conversation 为无参考对谈录屏",
    )
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    parser.add_argument("--asr-candidate", help="ASR 候选 ID，写入任务配置快照")
    parser.add_argument("--secondary-asr-candidate", help="第二 ASR 候选，串行运行并联合校对；空字符串关闭")
    parser.add_argument("--backend", choices=["codex_api", "agy", "codex_cli", "both"], help="覆盖阶段 6 使用的后端")
    parser.add_argument("--model", help="覆盖阶段 6 使用的模型，例如 gpt-6.1-sol")
    parser.add_argument("--reasoning-effort", help="覆盖阶段 6 reasoning effort，例如 low / medium / high")
    parser.add_argument("--ocr-model", help="覆盖 Codex API OCR 使用的模型，例如 gpt-6-luna")
    parser.add_argument("--ocr-reasoning-effort", help="覆盖 Codex API OCR reasoning effort，例如 low / medium / high")
    parser.add_argument("--ocr-max-concurrency", type=int, help="覆盖 PDF OCR 最大在途请求数")
    parser.add_argument("--ocr-submit-interval-seconds", type=float, help="覆盖 PDF OCR 页面投递间隔秒数")
    parser.add_argument("--book-name", help="可选书名，用于输出文件命名")
    parser.add_argument("--chapter", help="可选章节名，用于输出文件命名")
    parser.add_argument("--glossary-file", help="可选附加术语词表文件，一行一个词条")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.content_type == "book_club" and not (args.reference or "").strip():
        print("[ERROR] 读书会模式必须提供 --reference", file=sys.stderr)
        return 1
    if args.content_type == "conversation" and (args.reference or "").strip():
        print("[ERROR] 对谈模式不使用 --reference", file=sys.stderr)
        return 1

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
        result = run_single_job(
            project_root=PROJECT_ROOT,
            base_loaded_settings=loaded_settings,
            video=args.video,
            reference=args.reference,
            output_dir=args.output_dir,
            content_type=args.content_type,
            profile=args.profile,
            asr_candidate=args.asr_candidate,
            secondary_asr_candidate=args.secondary_asr_candidate,
            backend=args.backend,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            ocr_model=args.ocr_model,
            ocr_reasoning_effort=args.ocr_reasoning_effort,
            ocr_max_concurrency=args.ocr_max_concurrency,
            ocr_submit_interval_seconds=args.ocr_submit_interval_seconds,
            book_name=args.book_name,
            chapter=args.chapter,
            glossary_file=args.glossary_file,
        )
    except JobRunnerError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(f"[OK] job={result.job_id}")
    print(f"[OK] job_dir={result.job_root}")
    print(f"[OK] final_markdown={result.copied_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import ConfigLoadError, load_settings
from src.job_runner import JobRunnerError, get_batch_exit_code, load_batch_job_specs, run_batch_jobs
from src.settings_overrides import ModelOverrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按批量 job 输入契约运行完整主链。")
    parser.add_argument("--manifest", help="批量任务清单，支持 yaml/json")
    parser.add_argument("--videos-dir", help="视频目录")
    parser.add_argument("--reference-dir", help="参考原文目录，按 basename 配对")
    parser.add_argument("--shared-reference", help="共享参考源，本地 txt/md/pdf 或网页链接")
    parser.add_argument("--output-dir", help="最终 Markdown 输出目录")
    parser.add_argument(
        "--content-type",
        choices=["book_club", "conversation"],
        default="book_club",
        help="内容类型：book_club 为读书会，conversation 为无参考对谈录屏",
    )
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    parser.add_argument("--asr-candidate", help="ASR 候选 ID，写入每个任务配置快照")
    parser.add_argument("--secondary-asr-candidate", help="第二 ASR 候选；空字符串关闭双 ASR")
    parser.add_argument("--backend", choices=["codex_api", "agy", "codex_cli", "both"], help="覆盖阶段 6 使用的后端")
    parser.add_argument("--model", help="覆盖阶段 6 使用的模型，例如 gpt-6.1-sol")
    parser.add_argument("--reasoning-effort", help="覆盖阶段 6 reasoning effort，例如 low / medium / high")
    parser.add_argument("--ocr-model", help="覆盖 Codex API OCR 使用的模型，例如 gpt-6-luna")
    parser.add_argument("--ocr-reasoning-effort", help="覆盖 Codex API OCR reasoning effort，例如 low / medium / high")
    parser.add_argument("--ocr-max-concurrency", type=int, help="覆盖 PDF OCR 最大在途请求数")
    parser.add_argument("--ocr-submit-interval-seconds", type=float, help="覆盖 PDF OCR 页面投递间隔秒数")
    parser.add_argument("--glossary-file", help="批量默认术语词表文件，一行一个词条")
    parser.add_argument("--remote-concurrency", type=int, default=2, help="远程阶段并发度，默认 2")
    parser.add_argument("--book-name", help="批量默认书名，用于输出文件命名")
    parser.add_argument("--chapter", help="批量默认章节名，用于输出文件命名")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.remote_concurrency < 1:
        print("[ERROR] remote_concurrency 必须大于等于 1", file=sys.stderr)
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
        job_specs, failed_runtimes = load_batch_job_specs(
            base_loaded_settings=loaded_settings,
            manifest=args.manifest,
            videos_dir=args.videos_dir,
            reference_dir=args.reference_dir,
            shared_reference=args.shared_reference,
            output_dir=args.output_dir,
            content_type=args.content_type,
            book_name=args.book_name,
            chapter=args.chapter,
            glossary_file=args.glossary_file,
        )
        summary = run_batch_jobs(
            project_root=PROJECT_ROOT,
            base_loaded_settings=loaded_settings,
            job_specs=job_specs,
            failed_runtimes=failed_runtimes,
            remote_concurrency=args.remote_concurrency,
            backend_override=args.backend,
            model_overrides=ModelOverrides(
                asr_candidate=args.asr_candidate,
                secondary_asr_candidate=args.secondary_asr_candidate,
                llm_model=args.model,
                llm_reasoning_effort=args.reasoning_effort,
                ocr_model=args.ocr_model,
                ocr_reasoning_effort=args.ocr_reasoning_effort,
                ocr_max_concurrency=args.ocr_max_concurrency,
                ocr_submit_interval_seconds=args.ocr_submit_interval_seconds,
            ),
        )
    except JobRunnerError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    batch_root = PROJECT_ROOT / "data/jobs/batches" / summary.batch_id
    print(f"[OK] batch={summary.batch_id}")
    print(f"[OK] total={summary.total} success={summary.success} failed={summary.failed}")
    print(f"[OK] summary_json={batch_root / 'summary.json'}")
    return get_batch_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())

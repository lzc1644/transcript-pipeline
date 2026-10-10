from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.proofreading_memory import (
    MemoryExperimentError, MemoryStore, analyze_request, canonical,
    export_analysis_request, prepare_analysis, read_json, require,
    validate_metadata, validate_request, write_artifact, write_frozen_context,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立保真校对记忆实验，默认离线、不接入生产流水线。")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="显式三稿→离线自包含请求，不调用模型")
    for arg in ("asr", "system", "human", "metadata", "output"):
        prepare.add_argument("--" + arg, required=True)
    prepare.add_argument("--reference")
    prepare.add_argument("--reference-metadata")
    prepare.add_argument("--fragment-id", action="append", help="显式选择子集，工件记录遗漏ID")
    analyze = commands.add_parser("analyze", help="仅在 --allow-network 后将选中文本发到配置后端；不自动入库")
    analyze.add_argument("--request", required=True)
    analyze.add_argument("--output", required=True)
    analyze.add_argument("--allow-network", action="store_true")
    analyze.add_argument("--config")
    analyze.add_argument("--model")
    analyze.add_argument("--max-output-tokens", type=int)
    imported = commands.add_parser("import-response", help="校验本地响应，整批 pending 入库")
    for arg in ("request", "response", "db"):
        imported.add_argument("--" + arg, required=True)
    imported.add_argument("--response-mode", choices=("offline", "replay", "remote"), required=True)
    migration = commands.add_parser("migrate-v1", help="显式事务迁移已有v1库到v2，保留版本/审计并回填可恢复提案出处")
    migration.add_argument("--db", required=True)
    listing = commands.add_parser("list", help="检查当前版本及未独立审核的提案出处；出处不会自动进入context")
    listing.add_argument("--db", required=True)
    listing.add_argument("--status", choices=("pending", "approved", "rejected", "disabled"))
    for action in ("approve", "reject", "disable", "revise"):
        review = commands.add_parser(action, help="显式人工审核；每次状态改变生成不可变新版本")
        review.add_argument("memory_id")
        for arg in ("db", "reviewer", "reason"):
            review.add_argument("--" + arg, required=True)
        review.add_argument("--expected-version", type=int, required=True)
        if action == "revise":
            review.add_argument("--replacement", required=True, help="单个候选JSON（与原请求同一证据契约）")
    context = commands.add_parser("context", help="精确范围/数据集筛选并输出冻结上下文")
    for arg in ("db", "task-metadata", "query-file", "output"):
        context.add_argument("--" + arg, required=True)
    context.add_argument("--block-output")
    context.add_argument("--max-items", type=int, default=8)
    context.add_argument("--max-chars", type=int, default=4000)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            request = prepare_analysis(args.asr, args.system, args.human,
                                       metadata=read_json(args.metadata), reference_path=args.reference,
                                       reference_metadata=read_json(args.reference_metadata) if args.reference_metadata else None,
                                       selected_fragment_ids=args.fragment_id)
            export_analysis_request(request, args.output)
            result = {"request_id": request["request_id"], "coverage": request["coverage"], "output": args.output}
        elif args.command == "migrate-v1":
            result = MemoryStore.migrate_v1(args.db)
        elif args.command == "analyze":
            require(args.allow_network, "analysis requires explicit --allow-network; selected text will be sent to backend")
            require(not Path(args.output).exists() and not Path(args.output).is_symlink(), "refusing existing output")
            from src.config_loader import load_settings
            request = read_json(args.request)
            validate_request(request)
            response = analyze_request(request, allow_network=True, loaded_settings=load_settings(args.config),
                                       model=args.model, max_output_tokens=args.max_output_tokens)
            write_artifact(args.output, canonical(response) + "\n")
            result = {"candidate_count": len(response["candidates"]), "output": args.output, "stored": False}
        else:
            # Validate incoming packages before even creating a new DB on an invalid import.
            if args.command == "import-response":
                from src.proofreading_memory import validate_proposals
                request, response = read_json(args.request), read_json(args.response)
                validate_proposals(request, response)
            if args.command == "context":
                metadata = read_json(args.task_metadata)
                validate_metadata(metadata)
                query_path = Path(args.query_file)
                require(query_path.is_file() and query_path.stat().st_size <= 1024 * 1024, "query must be a file <=1 MiB")
                query = query_path.read_text(encoding="utf-8")
            with MemoryStore(args.db) as store:
                if args.command == "import-response":
                    result = store.import_candidates(request, response, response_mode=args.response_mode)
                elif args.command == "list":
                    result = store.list_entries(status=args.status)
                elif args.command == "context":
                    frozen = store.export_context(task_scope=metadata["scope"], dataset_kind=metadata["dataset_kind"],
                                                  query=query, max_items=args.max_items, max_chars=args.max_chars)
                    write_frozen_context(frozen, args.output, block_path=args.block_output)
                    result = {"selected_count": len(frozen["selected"]), "omitted_count": len(frozen["omitted"]),
                              "eligible_count": frozen["eligible_count"], "block_chars": frozen["block_chars"],
                              "notes": frozen["notes"], "fingerprint": frozen["fingerprint"], "output": args.output}
                elif args.command == "revise":
                    result = store.revise(args.memory_id, replacement=read_json(args.replacement), reviewer=args.reviewer,
                                          reason=args.reason, expected_version=args.expected_version)
                else:
                    result = store.review(args.memory_id, action=args.command, reviewer=args.reviewer,
                                          reason=args.reason, expected_version=args.expected_version)
        print(canonical(result))
        return 0
    except (MemoryExperimentError, OSError, UnicodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Configuration/SQLite failures may contain private values; do not dump settings or keys.
        print("[ERROR] configuration/database operation failed; no implicit fallback", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

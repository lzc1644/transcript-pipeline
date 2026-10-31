"""Isolated stage-6 experiment; never publishes or changes an existing job.

Prepare: .venv/bin/python scripts/probe_fulltext_refinement.py --job-id ID --output-dir tmp/NAME
Execute: repeat with --execute (one request; no automatic retry or model fallback).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config_loader import load_settings
from src.llm.gateway import GatewayClient
from src.llm.tasks.refinement import extract_json_payload
from src.request_trace import RequestTrace
from src.web.frontend_settings import gateway_connection_for_frontend, load_frontend_settings

# Keep the trial policy editable without changing the production cleanup prompt.
POLICY_PATH = ROOT / "config/prompts/fulltext_refinement_experiment.md"
POLICY = POLICY_PATH.read_text(encoding="utf-8")


def build_prompt(asr: str, reference: str) -> str:
    def numbered(text: str, prefix: str) -> str:
        return "\n".join(f"[{prefix} {i}] {line}" for i, line in enumerate(text.splitlines(), 1))
    return POLICY + "\n\n原始转录全文：\n" + numbered(asr, "ASR") + "\n\n参考附件全文：\n" + numbered(reference, "REF")


def valid_range(value: object, length: int) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and all(type(i) is int for i in value) and 1 <= value[0] <= value[1] <= length)


def validate_and_render(payload: dict, asr: str, reference: str) -> tuple[str, dict]:
    """Validate provenance shape, not semantic truth or the correctness of model labels."""
    errors, warnings = [], []
    lines, ref_lines = asr.splitlines(), reference.splitlines()
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return "", {"structural_passed": False, "errors": ["missing_blocks"], "warnings": []}
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or "\n" in title or title.strip() == "source":
        errors.append("invalid_title")
        title = "待复核试验稿"
    rendered, covered, counts = ["# " + title.strip()], set(), Counter()
    previous_end = 0
    for i, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            errors.append(f"block_{i}:not_object")
            continue
        kind, text = block.get("kind"), block.get("text")
        if kind not in {"quote", "speech", "heading"}:
            errors.append(f"block_{i}:invalid_kind")
            continue
        counts[kind] += 1
        role = block.get("interaction_role")
        if role is not None and (not isinstance(role, str) or role not in {"question", "answer", "comment"}):
            errors.append(f"block_{i}:invalid_interaction_role")
            role = None
        if role is not None and kind != "speech":
            errors.append(f"block_{i}:interaction_role_on_non_speech")
        source = block.get("source_lines")
        if not valid_range(source, len(lines)):
            errors.append(f"block_{i}:invalid_source_lines")
        else:
            if source[0] < previous_end:
                errors.append(f"block_{i}:source_order_or_overlap")
            previous_end = source[1]
            covered.update(range(source[0], source[1] + 1))
        ref = block.get("reference_lines")
        if kind == "quote":
            if ref is None:
                warnings.append(f"block_{i}:quote_without_reference")
            elif not valid_range(ref, len(ref_lines)):
                errors.append(f"block_{i}:invalid_reference_lines")
        elif ref is not None:
            errors.append(f"block_{i}:unexpected_reference_lines")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"block_{i}:empty_text")
            continue
        text = text.strip()
        if re.search(r"(?m)^\s*[>#]", text) or re.search(r"\[(?:ASR|REF) \d+\]", text):
            errors.append(f"block_{i}:embedded_structure_marker")
        if "OCR_PAGE_BREAK" in text or "\ufffd" in text:
            errors.append(f"block_{i}:leaked_or_damaged_text")
        if kind == "quote":
            rendered.append("\n".join("> " + line if line else ">" for line in text.splitlines()))
        elif kind == "heading":
            level = block.get("level", 2)
            if type(level) is not int or level not in (2, 3) or "\n" in text:
                errors.append(f"block_{i}:invalid_heading")
                level = 2
            if block.get("heading_type") == "interaction":
                if level != 2 or text != "问答与交流":
                    errors.append(f"block_{i}:invalid_interaction_heading")
                following = []
                for later in blocks[i:]:
                    if isinstance(later, dict) and later.get("kind") == "heading":
                        break
                    following.append(later)
                if not any(isinstance(later, dict) and later.get("kind") == "speech"
                           and later.get("interaction_role") in ("question", "answer", "comment")
                           and valid_range(later.get("source_lines"), len(lines)) for later in following):
                    errors.append(f"block_{i}:interaction_heading_without_following_interaction")
            rendered.append("#" * level + " " + text)
        else:
            labels = {"question": "提问", "answer": "回应", "comment": "交流发言"}
            if role is not None:
                if re.match(r"^(?:\*\*)?(?:提问|回答|回应|交流发言|互动)[：:]", text):
                    errors.append(f"block_{i}:embedded_interaction_label")
                rendered.append(f"**{labels[role]}：** {text}")
            else:
                rendered.append(text)
    omissions = payload.get("omissions")
    if not isinstance(omissions, list):
        errors.append("invalid_omissions")
        omissions = []
    omission_lines = set()
    for i, omission in enumerate(omissions, 1):
        if (not isinstance(omission, dict) or not valid_range(omission.get("source_lines"), len(lines))
                or not isinstance(omission.get("reason"), str) or not omission["reason"].strip()
                or not isinstance(omission.get("source_text"), str) or not omission["source_text"].strip()):
            errors.append(f"omission_{i}:invalid_record")
            continue
        start, end = omission["source_lines"]
        omission_lines.update(range(start, end + 1))
        if omission["source_text"] not in "\n".join(lines[start-1:end]):
            errors.append(f"omission_{i}:source_text_not_in_claimed_range")
    if not isinstance(payload.get("needs_review"), list):
        errors.append("invalid_needs_review")
    nonempty = {i for i, line in enumerate(lines, 1) if line.strip()}
    unexplained = sorted(nonempty - covered - omission_lines)
    if unexplained:
        errors.append("source_lines_unaccounted_for")
    report = {
        "structural_passed": not errors, "errors": errors, "warnings": warnings,
        "block_counts": dict(counts), "source_nonempty_lines": len(nonempty),
        "claimed_covered_lines": len(nonempty & covered),
        "claimed_omitted_lines": len(nonempty & omission_lines),
        "unaccounted_source_lines": unexplained,
        "semantic_acceptance": "requires_independent_reading_of_actual_source_and_candidate",
        "coverage_caveat": "Ranges are model claims; full line coverage does not prove text retention or correct boundaries.",
    }
    return "\n\n".join(rendered) + "\n", report


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.job_id):
        parser.error("invalid job ID")
    job = ROOT / "data/jobs" / args.job_id
    output = args.output_dir.resolve()
    # Results must not be written into jobs, production output, or any upstream tree.
    if not output.is_relative_to(ROOT / "tmp"):
        parser.error("output directory must be under project tmp/")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "request-started.json").exists():
        parser.error("request already started here; use a new output directory for a separately authorized attempt")
    asr = (job / "intermediate/asr/source.txt").read_text(encoding="utf-8")
    reference = (job / "intermediate/extracted_text/source.txt").read_text(encoding="utf-8")
    baseline = json.loads((job / "intermediate/refined/source.json").read_text(encoding="utf-8"))
    # Only model/thinking/tier are copied from the job; absolute /app paths are not applied locally.
    import yaml
    snapshot = yaml.safe_load((job / "settings.generated.yaml").read_text(encoding="utf-8"))["llm"]
    model, effort, fast = snapshot["model"], snapshot["reasoning_effort"], snapshot["fast_mode"]
    prompt = build_prompt(asr, reference)
    (output / "source-asr.txt").write_text(asr, encoding="utf-8")
    (output / "source-reference.txt").write_text(reference, encoding="utf-8")
    (output / "baseline.md").write_text(baseline["final_markdown"], encoding="utf-8")
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    manifest = {
        "job_id": args.job_id, "model": model, "reasoning_effort": effort, "fast_mode": fast,
        "asr_chars": len(asr), "reference_chars": len(reference), "prompt_chars": len(prompt),
        "policy_path": str(POLICY_PATH.relative_to(ROOT)),
        "baseline_backend_status": baseline["backend_status"],
        "changed": ["raw ASR without replacement/locking", "rewritten experimental policy", "line provenance", "structured output and programmatic Markdown"],
        "unchanged": ["ASR", "reference TXT", "model", "reasoning effort", "priority tier"],
        "remote_call_limit": 1, "production_writes": False,
    }
    write_json(output / "manifest.json", manifest)
    if not args.execute:
        print(json.dumps({"prepared": str(output), "remote_calls": 0}, ensure_ascii=False))
        return 0
    loaded = load_settings(project_root=ROOT)
    connection = gateway_connection_for_frontend(loaded.settings.codex_lb, load_frontend_settings(ROOT))
    if not connection.api_key:
        raise RuntimeError("No configured gateway credential; use the existing Web settings or configured key environment variable.")
    client = GatewayClient(connection, timeout_seconds=1800, require_stream_completed=True, total_timeout=True)
    payload = {
        "model": model, "reasoning": {"effort": effort}, "stream": True, "store": False,
        "instructions": "你是阶段 6 文稿校对助手。严格遵守用户输入中的 JSON 输出要求，只返回最终 JSON。",
        "input": prompt,
    }
    if fast:
        payload["service_tier"] = "priority"
    write_json(output / "request-started.json", {**manifest, "started_at": datetime.now(timezone.utc).isoformat()})
    started = time.monotonic()
    try:
        text = client.codex_responses_text(payload, request_trace=RequestTrace(output / "trace"))
        (output / "response.txt").write_text(text, encoding="utf-8")
        candidate = extract_json_payload(text)
        write_json(output / "candidate.json", candidate)
        markdown, report = validate_and_render(candidate, asr, reference)
        (output / "candidate.md").write_text(markdown, encoding="utf-8")
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_json(output / "validation.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["structural_passed"] else 2
    except Exception as exc:
        write_json(output / "failure.json", {"error_type": type(exc).__name__, "message": str(exc),
                                             "elapsed_seconds": round(time.monotonic()-started, 2)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())

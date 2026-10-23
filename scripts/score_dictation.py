#!/usr/bin/env python
"""Offline scoring of probe_dictation artifacts; never starts inference or modifies a run."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import re
from pathlib import Path
import unicodedata


def normalize(text):
    """Fixed v1: NFC/casefold; strip prose punctuation, retain numeric punctuation."""
    text = unicodedata.normalize("NFC", text).casefold()
    output = []
    for i, char in enumerate(text):
        numeric_mark = (char in ".,:/-%" and ((i > 0 and text[i - 1].isdigit()) or
                                              (i + 1 < len(text) and text[i + 1].isdigit())))
        if not char.isspace() and (numeric_mark or not unicodedata.category(char).startswith("P")):
            output.append(char)
    return "".join(output)


def literal_present(check, text):
    if text is None:
        return None
    expected, actual = normalize(check["expected"]), normalize(text)
    if check["kind"] == "number":
        # 1500 must not pass merely because 15000 contains it.
        return re.search(r"(?<![0-9.,:/+\-])" + re.escape(expected) + r"(?![0-9.,:/%])", actual) is not None
    return expected in actual


def edit_counts(reference, hypothesis):
    # O(hypothesis) memory, deterministic tie order substitution > deletion > insertion.
    previous = [(i, 0, 0, i) for i in range(len(hypothesis) + 1)]
    for i, a in enumerate(reference, 1):
        current = [(i, 0, i, 0)]
        for j, b in enumerate(hypothesis, 1):
            if a == b:
                current.append(previous[j - 1])
                continue
            choices = []
            for base, operation in ((previous[j - 1], 1), (previous[j], 2), (current[j - 1], 3)):
                values = list(base)
                values[0] += 1
                values[operation] += 1
                choices.append(tuple(values))
            current.append(min(enumerate(choices), key=lambda pair: (pair[1][0], pair[0]))[1])
        previous = current
    distance, substitutions, deletions, insertions = previous[-1]
    return {"substitutions": substitutions, "deletions": deletions, "insertions": insertions,
            "reference_chars": len(reference), "hypothesis_chars": len(hypothesis),
            "errors": distance, "cer": distance / len(reference) if reference else None,
            "silence_false_chars": len(hypothesis) if not reference else 0}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    low = int(index)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low)


def score(manifest, reports):
    ids = [item["id"] for item in manifest]
    if len(set(ids)) != len(ids):
        raise ValueError("样本ID不可重复")
    groups = {}
    for item in manifest:
        if item.get("split") not in {"tuning", "holdout"} or not item.get("recording_group"):
            raise ValueError("每个样本必须显式标记split=tuning/holdout和recording_group")
        group = item["recording_group"]
        if groups.setdefault(group, item["split"]) != item["split"]:
            raise ValueError("同一录音组不能分散到调参集和留出集")
    results, consumed = [], set()
    for item in manifest:
        matches = [(i, report) for i, report in enumerate(reports) if report.get("sample_id") == item["id"]]
        if not matches:
            source = Path(item["path"]).expanduser().resolve()
            matches = [(i, report) for i, report in enumerate(reports)
                       if not report.get("sample_id") and Path(report.get("source", "")).expanduser().resolve() == source]
        if len(matches) > 1:
            raise ValueError(f"样本{item['id']}有多份产物，请指定单一候选独立目录")
        report = matches[0][1] if matches else {}
        if matches:
            consumed.add(matches[0][0])
        state = report.get("result", {})
        utterances = state.get("utterances", [])
        raw = "\n".join(u.get("raw_text", "") for u in utterances if u.get("raw_text"))
        cleaned = "\n".join(u.get("cleaned_text", u.get("raw_text", "")) for u in utterances if u.get("raw_text"))
        document = state.get("document_text")
        reference = item.get("reference")
        if reference is not None and not isinstance(reference, str):
            raise ValueError("reference须为人工文本或null；静音参考使用空字符串")
        checks = []
        for check in item.get("checks", []):
            if check.get("kind") not in {"term", "number", "negation", "condition"} or not check.get("expected"):
                raise ValueError("检查项须有kind和非空expected；这些仅为字面检查，不冒充语义判断")
            checks.append({**check, "literal_only": True, "raw_present": literal_present(check, raw),
                           "cleaned_present": literal_present(check, cleaned),
                           "document_present": literal_present(check, document)})
        result = {"id": item["id"], "path": item["path"], "reference": reference,
                  "reference_provenance": item.get("reference_provenance"), "recording_group": item["recording_group"],
                  "split": item["split"], "categories": item.get("categories", []),
                  "status": state.get("status", "missing_report"), "error": state.get("error"),
                  "source_artifact": report.get("session_directory"), "realtime": report.get("realtime", False),
                  "received_audio_seconds": report.get("received_audio_seconds"), "config": state.get("config", {}),
                  "raw_asr": raw, "sentence_cleanup": cleaned, "document_cleanup": document,
                  "raw_score": edit_counts(normalize(reference), normalize(raw)) if reference is not None else None,
                  "checks": checks,
                  "semantic_review": item.get("semantic_review"),  # Human per-layer decisions, not a CER proxy.
                  "asr_failed": sum(u.get("status") == "asr_failed" for u in utterances),
                  "cleanup_failed": sum(u.get("status") == "cleanup_failed" for u in utterances),
                  "duplicate_ids": [uid for uid, n in Counter(u["id"] for u in utterances).items() if n > 1],
                  "metrics": state.get("metrics", {}),
                  "latency": {key: [u[key] for u in utterances if key in u] for key in
                              ("first_partial_seconds", "final_latency_seconds", "cleanup_latency_seconds", "asr_seconds")}}
        results.append(result)
    if len(consumed) != len(reports):
        raise ValueError("存在不在清单中的运行产物；不能静默丢弃失败或多余样本")
    summary = {"samples": len(results), "scored_samples": sum(r["raw_score"] is not None for r in results),
               "missing_reports": sum(r["status"] == "missing_report" for r in results),
               "failed_or_incomplete_samples": sum(r["status"] != "completed" for r in results),
               "human_semantic_reviewed": sum(bool(r["semantic_review"]) for r in results),
               "normalization": "v1: NFC + casefold; remove prose punctuation/space; preserve numeric punctuation, digit forms and words",
               "decision": "保留原默认配置；仅离线评分，准确率改善需同源基线/单变量候选及独立留出与人工语义复核"}
    summary["groups"] = {}
    for group in ("all", "tuning", "holdout"):
        selected = [r for r in results if group == "all" or r["split"] == group]
        scores = [r["raw_score"] for r in selected if r["raw_score"] is not None]
        total = {key: sum(s[key] for s in scores) for key in
                 ("reference_chars", "errors", "substitutions", "deletions", "insertions", "silence_false_chars")}
        total["cer"] = total["errors"] / total["reference_chars"] if total["reference_chars"] else None
        summary["groups"][group] = {"samples": len(selected), "scored": len(scores), **total}
    summary["latency"] = {}
    for key in ("first_partial_seconds", "final_latency_seconds", "cleanup_latency_seconds", "asr_seconds"):
        # Sample-clock latency only from explicit 1x reports; inference duration may come from fast file runs.
        values = [v for r in results if r["realtime"] or key == "asr_seconds" for v in r["latency"][key]]
        summary["latency"][key] = {"count": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
    return {"summary": summary, "samples": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True, help="one probe_dictation output directory")
    parser.add_argument("--output", type=Path, required=True, help="new independent score directory")
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in sorted(args.run.glob("[0-9][0-9][0-9].json"))]
    result = score(json.loads(args.manifest.read_text()), reports)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "scores.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

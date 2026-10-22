#!/usr/bin/env python
"""Bounded real-channel cleanup comparison. No automatic default/model fallback."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config_loader import load_settings
from src.dictation.cleanup import Cleanup, INSTRUCTIONS, OUTPUT_FORMAT, UsageTrace, validate_cleanup
from src.web.frontend_settings import load_frontend_settings
from scripts.probe_dictation import percentile

CANDIDATES = {"gpt-6-luna": "low", "gemini-3.5-flash-lite": "minimal", "gemini-3.8-flash-high": "low"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path, help="JSON list of {text, expected?}")
    parser.add_argument("--models", nargs="+", choices=list(CANDIDATES), default=list(CANDIDATES))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--document", action="store_true", help="Use the production Luna high whole-document strategy")
    args = parser.parse_args()
    if args.document and args.models != ["gpt-6-luna"]:
        parser.error("--document requires --models gpt-6-luna")
    args.output.mkdir(parents=True, exist_ok=False)
    loaded = load_settings(project_root=ROOT)
    frontend = load_frontend_settings(ROOT)
    gateway = loaded.settings.codex_lb
    gateway = gateway.model_copy(update={"base_url": os.getenv(gateway.base_url_env, "") or frontend.codex_lb_base_url or gateway.base_url,
                                        "base_url_env": ""})
    client = Cleanup(gateway, timeout=30, project_root=ROOT).client
    records = []
    for model in args.models:
        for sample in json.loads(args.manifest.read_text()):
            started = time.monotonic()
            record = {"requested_model": model, "channel": "http-responses", "reasoning_effort": "high" if args.document else CANDIDATES[model],
                      "input": sample["text"], "expected": sample.get("expected"),
                      "actual_model_reported": None, "input_tokens": None, "output_tokens": None}
            if model.startswith("gemini-3.8"):
                record["channel_caveat"] = "Only -high alias is advertised by the gateway; LOW forwarding is not assumed verified."
            usage = UsageTrace()
            try:
                if args.document or model == "gpt-6-luna":
                    cleaner = Cleanup(gateway, project_root=ROOT)
                    function = cleaner.document if args.document else cleaner
                    result = function(sample["text"], [], sample.get("terms", []))
                    requests = result["metrics"].get("requests", [result["metrics"]])
                    usage.metadata.update(reported_model=requests[-1]["reported_model"],
                        input_tokens=sum(r["input_tokens"] for r in requests) if all(type(r["input_tokens"]) is int for r in requests) else None,
                        output_tokens=sum(r["output_tokens"] for r in requests) if all(type(r["output_tokens"]) is int for r in requests) else None)
                    record.update(status="ok", result=result)
                else:
                    response = client.codex_responses_text({"model": model, "reasoning": {"effort": CANDIDATES[model]},
                    "instructions": INSTRUCTIONS, "input": json.dumps({"current_text": sample["text"], "context": [], "terms": []}, ensure_ascii=False),
                        "stream": True, "store": False, "max_output_tokens": 4096, "text": OUTPUT_FORMAT}, request_trace=usage)
                    record.update(status="ok", result=validate_cleanup(response))
            except Exception as exc:
                record.update(status="invalid_response" if isinstance(exc, ValueError) else "unavailable", error=str(exc))
                if isinstance(exc, ValueError) and "response" in locals():
                    record["invalid_output_excerpt"] = response[:1000]
            record.update(input_tokens=usage.metadata["input_tokens"], output_tokens=usage.metadata["output_tokens"],
                          actual_model_reported=usage.metadata["reported_model"])
            record["complete_seconds"] = time.monotonic() - started
            records.append(record)
            if record["status"] != "ok":
                break
    summary = {}
    for model in args.models:
        values = [r["complete_seconds"] for r in records if r["requested_model"] == model and r["status"] == "ok"]
        summary[model] = {"successful_calls": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
    result = {"records": records, "summary": summary,
              "agy_available": bool(shutil.which("agy")),
              "quality_acceptance": "Manual fidelity review required; successful parsing is not quality acceptance."}
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

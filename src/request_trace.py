from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger("transcript_pipeline")
TRACE_SCHEMA_VERSION = 1


def trace_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def create_trace_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"run-{timestamp}-{uuid.uuid4().hex}"


def safe_trace_component(value: str) -> str:
    normalized = re.sub(r"[^\w.\-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return normalized or "unknown"


def endpoint_metadata(raw_url: str) -> dict[str, Any]:
    try:
        parsed = urlparse(raw_url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        # 诊断元数据解析失败不能覆盖原始网络异常；异常 URL 也不原样落盘，避免带出查询参数。
        return {"scheme": "", "host": "", "port": None, "path": ""}
    return {
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "path": parsed.path,
    }


def sanitized_proxy_environment() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        raw_value = os.environ.get(name, "").strip()
        if not raw_value:
            continue
        if name.lower() == "no_proxy":
            result[name] = raw_value
            continue
        try:
            parsed = urlparse(raw_value)
            host = parsed.hostname
            parsed_port = parsed.port
        except ValueError:
            result[name] = "configured"
            continue
        if not parsed.scheme or not host:
            result[name] = "configured"
            continue
        port = f":{parsed_port}" if parsed_port is not None else ""
        result[name] = f"{parsed.scheme}://{host}{port}"
    return result


def text_fingerprint(text: str) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {
        "chars": len(text),
        "utf8_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def exception_metadata(exc: BaseException, *, category: str) -> dict[str, Any]:
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "recorded_at": trace_now_iso(),
        "category": category,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


@dataclass(frozen=True)
class RequestTrace:
    directory: Path

    def path_for(self, filename: str) -> Path:
        return self.directory / filename

    def write_text(self, filename: str, content: str) -> bool:
        path = self.path_for(filename)
        pending_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.pending")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(content, encoding="utf-8")
            pending_path.replace(path)
            return True
        except OSError as exc:
            # 诊断落盘是旁路能力；磁盘权限或空间问题必须显式告警，但不能把合法模型结果改判为失败。
            LOGGER.warning("请求诊断文本写入失败 | path=%s | %s", path, exc)
            return False
        finally:
            try:
                if pending_path.exists():
                    pending_path.unlink()
            except OSError as exc:
                LOGGER.warning("请求诊断临时文件清理失败 | path=%s | %s", pending_path, exc)

    def write_json(self, filename: str, payload: dict[str, Any]) -> bool:
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            # 诊断序列化同样属于旁路，必须告警而不能改变正常请求结果。
            LOGGER.warning("请求诊断 JSON 序列化失败 | path=%s | %s", self.path_for(filename), exc)
            return False
        return self.write_text(filename, content)

    def read_json(self, filename: str) -> dict[str, Any]:
        path = self.path_for(filename)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 已损坏的诊断索引不应阻断正常校对，但 warning 会保留真实问题，避免静默伪装成功。
            LOGGER.warning("请求诊断 JSON 读取失败 | path=%s | %s", path, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def record_transport(self, record: dict[str, Any], *, raw_response: str) -> None:
        payload = self.read_json("transport.json")
        raw_transports = payload.get("transports", [])
        transports = list(raw_transports) if isinstance(raw_transports, list) else []
        sequence = len(transports) + 1
        transport_name = safe_trace_component(str(record.get("transport") or "unknown"))
        raw_filename = f"raw-response-{sequence:02d}-{transport_name}.txt"
        self.write_text(raw_filename, raw_response)
        self.write_text("raw-response.sse", raw_response)
        transports.append({**record, "sequence": sequence, "raw_response_file": raw_filename})
        self.write_json(
            "transport.json",
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "updated_at": trace_now_iso(),
                "transports": transports,
            },
        )


def build_refine_request_trace(
    logs_dir: Path,
    *,
    basename: str,
    backend: str,
    run_id: str,
    attempt: int,
) -> RequestTrace:
    directory = (
        logs_dir
        / "refine"
        / safe_trace_component(basename)
        / safe_trace_component(backend)
        / safe_trace_component(run_id)
        / f"attempt-{attempt:03d}"
    )
    return RequestTrace(directory)


def describe_refine_attempt(
    trace: RequestTrace,
    *,
    reasons: list[str],
    markdown: str = "",
    error: BaseException | None = None,
    refinement_reason: str | None = None,
) -> dict[str, Any]:
    """把根因而非回退副作用放入汇总；候选稿仅保存到诊断目录。"""
    details: dict[str, Any] = {
        "requested_model": trace.read_json("request-meta.json").get("model"),
    }
    if not reasons and error is None:
        return {**details, "failure_category": None, "failure_summary": None}

    if error is None and (
        refinement_reason == "locked_quote_changed" or "locked_quote_changed" in reasons
    ):
        return {
            **details,
            "failure_category": "locked_quote_validation",
            "failure_summary": "模型结果未完整保留锁定原文的实词或顺序（locked_quote_changed），不是后端调用失败。",
            "failure_evidence_file": "locked-quote-validation.json",
        }

    if error is not None or "programmatic_markdown_fallback" in reasons:
        for filename, category, label in (
            ("fallback-backend-error.json", "backend", "备用后端失败"),
            ("parse-error.json", "model_output_json", "模型正文 JSON 解析失败"),
            ("result-contract-error.json", "result_contract", "模型结果结构不符合要求"),
            ("backend-error.json", "transport_or_protocol", "API 传输或响应协议失败"),
            ("primary-backend-error.json", "backend", "后端调用失败"),
        ):
            evidence = trace.read_json(filename)
            if evidence.get("message"):
                return {
                    **details,
                    "failure_category": category,
                    "failure_summary": f"{label}：{evidence['message']}",
                    "failure_evidence_file": filename,
                }
        return {
            **details,
            "failure_category": "backend",
            "failure_summary": str(error) if error is not None else "后端失败后生成程序回退稿；未记录更具体的底层错误。",
        }

    labels = {
        "contains_unicode_replacement_character": "正文包含 Unicode 替换字符 �，需要人工复核",
        "uses_canonical_source_placeholder_title": "正文使用内部占位标题 # source",
    }
    details.update({
        "failure_category": "content_validation",
        "failure_summary": "；".join(labels.get(reason, reason) for reason in reasons),
    })
    if markdown.strip():
        positions = [index for index, char in enumerate(markdown) if char == "\ufffd"]
        details["replacement_character_count"] = len(positions)
        details["replacement_character_examples"] = [
            {
                "offset": index,
                "line": markdown.count("\n", 0, index) + 1,
                "context": markdown[max(0, index - 40):index + 41],
            }
            for index in positions[:10]
        ]
        # 不覆盖 refined / final，不把程序回退稿标成模型候选稿。
        if trace.write_text("candidate-review.md", markdown):
            details["candidate_file"] = "candidate-review.md"
            details["candidate_status"] = "needs_review_not_accepted"
    return details


def update_refine_diagnostics_summary(logs_dir: Path, entry: dict[str, Any]) -> None:
    root_trace = RequestTrace(logs_dir / "refine")
    payload = root_trace.read_json("diagnostics.json")
    raw_attempts = payload.get("attempts", [])
    attempts = list(raw_attempts) if isinstance(raw_attempts, list) else []
    entry_key = (
        str(entry.get("run_id") or ""),
        str(entry.get("file") or ""),
        str(entry.get("backend") or ""),
        int(entry.get("attempt") or 0),
    )
    replaced = False
    for index, current in enumerate(attempts):
        if not isinstance(current, dict):
            continue
        current_key = (
            str(current.get("run_id") or ""),
            str(current.get("file") or ""),
            str(current.get("backend") or ""),
            int(current.get("attempt") or 0),
        )
        if current_key == entry_key:
            attempts[index] = entry
            replaced = True
            break
    if not replaced:
        attempts.append(entry)
    root_trace.write_json(
        "diagnostics.json",
        {
            "schema_version": TRACE_SCHEMA_VERSION,
            "stage": "refine",
            "updated_at": trace_now_iso(),
            "attempts": attempts,
        },
    )

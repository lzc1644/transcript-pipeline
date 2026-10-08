from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.request_trace import RequestTrace, endpoint_metadata, exception_metadata, trace_now_iso
from src.schemas import CodexLBSettings


class CodexLBClientError(RuntimeError):
    """CPA / OpenAI 兼容网关 API 调用失败（保留类名以兼容旧调用）。"""


@dataclass(frozen=True)
class CodexLBClient:
    settings: CodexLBSettings
    timeout_seconds: float | None = None

    @property
    def base_url(self) -> str:
        env_name = self.settings.base_url_env.strip()
        env_value = os.environ.get(env_name, "").strip() if env_name else ""
        base_url = env_value or self.settings.base_url.strip()
        if not base_url:
            raise CodexLBClientError("API 网关 base_url 为空，请检查 codex_lb.base_url 或配置的地址环境变量。")
        return base_url.rstrip("/")

    @property
    def api_key(self) -> str:
        env_name = self.settings.api_key_env.strip()
        api_key = os.environ.get(env_name, "").strip() if env_name else ""
        if not api_key:
            raise CodexLBClientError(f"缺少 API 网关 API Key 环境变量: {env_name or 'CODEX_LB_API_KEY'}")
        return api_key

    def responses_text(self, payload: dict[str, Any]) -> str:
        response_payload = self.post_json(self.settings.responses_path, payload, label="Responses API")
        return extract_response_text(response_payload)

    def responses_stream_text(
        self,
        payload: dict[str, Any],
        *,
        request_trace: RequestTrace | None = None,
    ) -> str:
        return self.post_event_stream(
            self.settings.responses_path,
            payload,
            label="Responses API",
            request_trace=request_trace,
        )

    def codex_responses_text(
        self,
        payload: dict[str, Any],
        *,
        request_trace: RequestTrace | None = None,
    ) -> str:
        return self.post_event_stream(
            self.settings.codex_responses_path,
            payload,
            label="Responses API",
            request_trace=request_trace,
        )

    def upload_file(self, file_path: Path, *, use_case: str = "codex") -> str:
        try:
            file_bytes = file_path.read_bytes()
        except OSError as exc:
            raise CodexLBClientError(f"无法读取待上传文件: {file_path} | {exc}") from exc

        create_payload = {
            "file_name": file_path.name,
            "file_size": len(file_bytes),
            "use_case": use_case,
        }
        create_result = self.post_json(self.settings.files_create_path, create_payload, label="文件注册")
        file_id = create_result.get("file_id")
        upload_url = create_result.get("upload_url")
        if not isinstance(file_id, str) or not file_id:
            raise CodexLBClientError(f"文件注册响应缺少 file_id: {create_result}")
        if not isinstance(upload_url, str) or not upload_url:
            raise CodexLBClientError(f"文件注册响应缺少 upload_url: {create_result}")

        self.put_file_bytes(upload_url, file_bytes, label="文件直传")
        finalize_path = self.settings.files_finalize_path_template.format(file_id=file_id)
        finalize_result = self.post_json(finalize_path, {}, label="文件上传完成确认")
        status = finalize_result.get("status")
        if status != "success":
            raise CodexLBClientError(f"文件上传完成确认失败: file_id={file_id}, status={status}, payload={finalize_result}")
        return file_id

    def post_json(self, path: str, payload: dict[str, Any], *, label: str) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            endpoint_url(self.base_url, path),
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return parse_json_response(
            read_http_response(
                request,
                label=label,
                timeout_seconds=self.timeout_seconds,
                use_curl_first=should_use_curl_first(self.base_url),
            ),
            label=label,
        )

    def post_event_stream(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        label: str,
        request_trace: RequestTrace | None = None,
    ) -> str:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            endpoint_url(self.base_url, path),
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": "codex_cli_rs/0.130.0 transcript-pipeline",
            },
        )
        stream_text = read_http_response(
            request,
            label=label,
            timeout_seconds=self.timeout_seconds,
            use_curl_first=should_use_curl_first(self.base_url),
            request_trace=request_trace,
        )
        if request_trace is not None:
            request_trace.write_json("sse-summary.json", summarize_event_stream(stream_text))
        try:
            output_text = extract_event_stream_text(stream_text)
        except CodexLBClientError as exc:
            if request_trace is not None:
                request_trace.write_json(
                    "protocol-error.json",
                    exception_metadata(exc, category="sse_protocol"),
                )
            raise
        if request_trace is not None:
            request_trace.write_text("extracted-output.txt", output_text)
        return output_text

    def put_file_bytes(self, upload_url: str, file_bytes: bytes, *, label: str) -> None:
        request = Request(
            upload_url,
            data=file_bytes,
            method="PUT",
            headers={
                # Azure Blob SAS 上传需要这个头；该要求来自 Codex 官方文件上传实现。
                "x-ms-blob-type": "BlockBlob",
                "Content-Length": str(len(file_bytes)),
            },
        )
        read_http_response(
            request,
            label=label,
            timeout_seconds=self.timeout_seconds,
            use_curl_first=should_use_curl_first(upload_url),
        )


def endpoint_url(base_url: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    base_url = base_url.rstrip("/")
    path = "/" + path.lstrip("/")
    # CPA 常见 Base URL 带 /v1；根地址也继续可用，反代路径前缀不变。
    if urlparse(base_url).path.endswith("/v1") and path.startswith("/v1/"):
        path = path[len("/v1"):]
    return f"{base_url}{path}"


def read_http_response(
    request: Request,
    *,
    label: str,
    timeout_seconds: float | None,
    use_curl_first: bool = False,
    request_trace: RequestTrace | None = None,
) -> str:
    if use_curl_first:
        return read_http_response_with_curl(
            request,
            label=label,
            timeout_seconds=timeout_seconds,
            request_trace=request_trace,
        )

    started_at = trace_now_iso()
    started = time.monotonic()
    try:
        if timeout_seconds is None:
            with urlopen(request) as response:
                response_bytes = response.read()
                status_code = getattr(response, "status", None)
                effective_url = response.geturl() if hasattr(response, "geturl") else request.full_url
        else:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_bytes = response.read()
                status_code = getattr(response, "status", None)
                effective_url = response.geturl() if hasattr(response, "geturl") else request.full_url
        response_text = response_bytes.decode("utf-8", errors="replace")
        record_transport(
            request_trace,
            request=request,
            raw_response=response_text,
            record={
                "transport": "urllib",
                "result": "ok",
                "started_at": started_at,
                "finished_at": trace_now_iso(),
                "duration_seconds": round(time.monotonic() - started, 6),
                "http_status": status_code,
                "endpoint": endpoint_metadata(effective_url),
                "response_chars": len(response_text),
                "response_utf8_bytes": len(response_text.encode("utf-8")),
            },
        )
        return response_text
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        safe_body = redact_request_secrets(body, request)
        record_transport(
            request_trace,
            request=request,
            raw_response=body,
            record={
                "transport": "urllib",
                "result": "http_error",
                "started_at": started_at,
                "finished_at": trace_now_iso(),
                "duration_seconds": round(time.monotonic() - started, 6),
                "http_status": exc.code,
                "endpoint": endpoint_metadata(getattr(exc, "url", request.full_url)),
                "error_type": type(exc).__name__,
                "message": str(exc.reason),
            },
        )
        if should_retry_with_curl(exc.code, body):
            return read_http_response_with_curl(
                request,
                label=label,
                timeout_seconds=timeout_seconds,
                request_trace=request_trace,
            )
        raise CodexLBClientError(f"{label} HTTP {exc.code}: {safe_body or exc.reason}") from exc
    except IncompleteRead as exc:
        partial = exc.partial.decode("utf-8", errors="replace") if isinstance(exc.partial, bytes) else str(exc.partial)
        record_transport(
            request_trace,
            request=request,
            raw_response=partial,
            record=transport_exception_record(
                transport="urllib",
                result="incomplete_read",
                started_at=started_at,
                started=started,
                request=request,
                exc=exc,
            ),
        )
        raise CodexLBClientError(f"{label} 响应未完整传输: {exc}") from exc
    except HTTPException as exc:
        record_transport(
            request_trace,
            request=request,
            raw_response="",
            record=transport_exception_record(
                transport="urllib",
                result="protocol_error",
                started_at=started_at,
                started=started,
                request=request,
                exc=exc,
            ),
        )
        raise CodexLBClientError(f"{label} HTTP 协议失败: {exc}") from exc
    except URLError as exc:
        record_transport(
            request_trace,
            request=request,
            raw_response="",
            record=transport_exception_record(
                transport="urllib",
                result="url_error",
                started_at=started_at,
                started=started,
                request=request,
                exc=exc,
            ),
        )
        raise CodexLBClientError(f"{label} 请求失败: {exc}") from exc
    except OSError as exc:
        record_transport(
            request_trace,
            request=request,
            raw_response="",
            record=transport_exception_record(
                transport="urllib",
                result="os_error",
                started_at=started_at,
                started=started,
                request=request,
                exc=exc,
            ),
        )
        raise CodexLBClientError(f"{label} 请求失败: {exc}") from exc


def should_retry_with_curl(status_code: int, body: str) -> bool:
    if status_code != 403 or shutil.which("curl") is None:
        return False
    normalized = body.lower()
    return (
        "browser_signature_banned" in normalized
        or "cloudflare" in normalized
        or "error code: 1010" in normalized
        or "your request was blocked" in normalized
    )


def should_use_curl_first(url: str) -> bool:
    if shutil.which("curl") is None:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host not in {"", "127.0.0.1", "localhost", "::1"} and not host.endswith(".local")


def read_http_response_with_curl(
    request: Request,
    *,
    label: str,
    timeout_seconds: float | None,
    request_trace: RequestTrace | None = None,
) -> str:
    # 用户的远程 codex-lb 反代域会用 Cloudflare 1010 拦截 Python urllib 的 TLS/客户端指纹；
    # curl 已验证可通过同一 API 入口和远程文件上传 URL，因此远程地址优先使用 curl，本地仍走 urllib。
    body_path: Path | None = None
    started_at = trace_now_iso()
    started = time.monotonic()
    try:
        if request.data is not None:
            with tempfile.NamedTemporaryFile(delete=False) as body_file:
                body_file.write(request.data)
                body_path = Path(body_file.name)

        command = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--request",
            request.get_method(),
            "--config",
            "-",
            "--output",
            "-",
            "--write-out",
            build_curl_write_out_format(),
        ]
        if timeout_seconds is not None:
            command.extend(["--max-time", str(timeout_seconds)])
        if body_path is not None:
            command.extend(["--data-binary", f"@{body_path}"])
        command.append(request.full_url)

        try:
            completed = subprocess.run(
                command,
                input=build_curl_header_config(dict(request.header_items())).encode("utf-8"),
                text=False,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            record_transport(
                request_trace,
                request=request,
                raw_response="",
                record=transport_exception_record(
                    transport="curl",
                    result="curl_launch_error",
                    started_at=started_at,
                    started=started,
                    request=request,
                    exc=exc,
                ),
            )
            raise CodexLBClientError(f"{label} 无法启动 curl: {exc}") from exc
    finally:
        if body_path is not None:
            try:
                body_path.unlink()
            except OSError:
                pass

    stdout = decode_subprocess_output(completed.stdout)
    raw_stderr = decode_subprocess_output(completed.stderr)
    try:
        response_text, metrics = split_curl_response_metrics(stdout)
    except CodexLBClientError as exc:
        response_text = stdout
        metrics = {}
        if completed.returncode == 0:
            record_transport(
                request_trace,
                request=request,
                raw_response=response_text,
                record={
                    **transport_exception_record(
                        transport="curl",
                        result="curl_metadata_error",
                        started_at=started_at,
                        started=started,
                        request=request,
                        exc=exc,
                    ),
                    "return_code": completed.returncode,
                    "stderr": redact_request_secrets(raw_stderr.strip(), request),
                },
            )
            raise CodexLBClientError(f"{label} curl 状态信息解析失败: {exc}") from exc

    status_text = str(metrics.get("http_code") or "")
    status_code = int(status_text) if status_text.isdigit() else None
    stderr = redact_request_secrets(raw_stderr.strip(), request)
    common_record = {
        "transport": "curl",
        "started_at": started_at,
        "finished_at": trace_now_iso(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "return_code": completed.returncode,
        "stderr": stderr,
        "http_status": status_code,
        "endpoint": endpoint_metadata(request.full_url),
        "metrics": metrics,
        "response_chars": len(response_text),
        "response_utf8_bytes": len(response_text.encode("utf-8")),
    }
    if completed.returncode != 0:
        record_transport(
            request_trace,
            request=request,
            raw_response=response_text,
            record={**common_record, "result": "curl_error"},
        )
        detail = stderr or response_text.strip() or f"curl exited with code {completed.returncode}"
        raise CodexLBClientError(f"{label} curl 请求失败: {detail}")

    if status_code is None:
        record_transport(
            request_trace,
            request=request,
            raw_response=response_text,
            record={**common_record, "result": "curl_status_error"},
        )
        raise CodexLBClientError(f"{label} curl 状态码解析失败: {status_text}")
    if status_code >= 400:
        record_transport(
            request_trace,
            request=request,
            raw_response=response_text,
            record={**common_record, "result": "http_error"},
        )
        raise CodexLBClientError(
            f"{label} HTTP {status_code}: {redact_request_secrets(response_text, request)}"
        )
    record_transport(
        request_trace,
        request=request,
        raw_response=response_text,
        record={**common_record, "result": "ok"},
    )
    return response_text


CURL_METRIC_FIELDS = (
    "http_code",
    "remote_ip",
    "remote_port",
    "local_ip",
    "local_port",
    "http_version",
    "time_namelookup",
    "time_connect",
    "time_appconnect",
    "time_starttransfer",
    "time_total",
    "size_download",
)


def build_curl_write_out_format() -> str:
    return "\n" + "\t".join(f"%{{{field}}}" for field in CURL_METRIC_FIELDS)


def decode_subprocess_output(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def split_curl_response_metrics(stdout: str) -> tuple[str, dict[str, str]]:
    if "\n" not in stdout:
        raise CodexLBClientError("curl 响应缺少传输状态信息。")
    response_text, metadata_text = stdout.rsplit("\n", 1)
    values = metadata_text.split("\t")
    # 兼容旧测试桩及旧 curl 输出；真实请求使用上面的完整字段格式。
    if len(values) == 1:
        return response_text, {"http_code": values[0]}
    if len(values) != len(CURL_METRIC_FIELDS):
        raise CodexLBClientError(f"curl 传输状态字段数量异常: {len(values)}")
    return response_text, dict(zip(CURL_METRIC_FIELDS, values, strict=True))


def build_curl_header_config(headers: dict[str, str]) -> str:
    lines = []
    for key, value in headers.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'header = "{key}: {escaped}"')
    return "\n".join(lines) + "\n"


def split_curl_response(stdout: str) -> tuple[str, str]:
    response_text, metrics = split_curl_response_metrics(stdout)
    return response_text, str(metrics.get("http_code") or "")


def transport_exception_record(
    *,
    transport: str,
    result: str,
    started_at: str,
    started: float,
    request: Request,
    exc: BaseException,
) -> dict[str, Any]:
    return {
        "transport": transport,
        "result": result,
        "started_at": started_at,
        "finished_at": trace_now_iso(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "endpoint": endpoint_metadata(request.full_url),
        "error_type": type(exc).__name__,
        "message": redact_request_secrets(str(exc), request),
    }


def redact_request_secrets(text: str, request: Request) -> str:
    redacted = text
    for key, value in request.header_items():
        if key.casefold() not in {"authorization", "proxy-authorization", "x-api-key", "api-key"}:
            continue
        candidates = [value]
        if " " in value:
            candidates.append(value.split(" ", 1)[1])
        for candidate in candidates:
            if candidate:
                redacted = redacted.replace(candidate, "[REDACTED]")
    return redacted


def record_transport(
    request_trace: RequestTrace | None,
    *,
    request: Request,
    raw_response: str,
    record: dict[str, Any],
) -> None:
    if request_trace is None:
        return
    request_trace.record_transport(
        record,
        raw_response=redact_request_secrets(raw_response, request),
    )


def parse_json_response(text: str, *, label: str) -> dict[str, Any]:
    if not text.strip():
        raise CodexLBClientError(f"{label} 返回为空。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexLBClientError(f"{label} JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise CodexLBClientError(f"{label} JSON 顶层必须是对象。")
    return payload


def find_response_text(payload: dict[str, Any]) -> str | None:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    if isinstance(output_text, list):
        parts = [item for item in output_text if isinstance(item, str) and item]
        if parts:
            return "".join(parts).strip()

    parts: list[str] = []
    output_items = payload.get("output")
    if isinstance(output_items, list):
        for item in output_items:
            if not isinstance(item, dict):
                continue
            collect_text_parts(item.get("content"), parts)
            collect_text_parts(item, parts)

    text = "".join(parts).strip()
    return text or None


def extract_response_text(payload: dict[str, Any]) -> str:
    text = find_response_text(payload)
    if text is None:
        raise CodexLBClientError(f"Responses API 返回中未找到 output_text: {payload}")
    return text


def extract_event_stream_text(stream_text: str) -> str:
    parts: list[str] = []
    completed_text: str | None = None
    saw_output_text = False
    failed_payload: dict[str, Any] | None = None
    for block in iter_sse_blocks(stream_text):
        event_name, data_text = parse_sse_block(block)
        if not data_text or data_text == "[DONE]":
            continue
        try:
            payload = json.loads(data_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or event_name or "")
        if event_type in {"response.output_text.delta", "response.text.delta"}:
            delta = payload.get("delta")
            if isinstance(delta, str):
                parts.append(delta)
            continue
        if event_type == "response.output_text.done":
            output_text = payload.get("text")
            if isinstance(output_text, str):
                saw_output_text = True
                if output_text.strip():
                    completed_text = output_text.strip()
            continue
        if event_type == "response.content_part.done":
            if contains_output_text_part(payload.get("part")):
                saw_output_text = True
            content_parts: list[str] = []
            collect_text_parts(payload.get("part"), content_parts)
            candidate = "".join(content_parts).strip()
            if candidate:
                completed_text = candidate
            continue
        if event_type == "response.output_item.done":
            item = payload.get("item")
            item_parts: list[str] = []
            if isinstance(item, dict):
                if contains_output_text_part(item.get("content")):
                    saw_output_text = True
                collect_text_parts(item.get("content"), item_parts)
                collect_text_parts(item, item_parts)
            candidate = "".join(item_parts).strip()
            if candidate:
                completed_text = candidate
            continue
        if event_type in {"response.failed", "response.incomplete"}:
            failed_payload = payload
            continue
        if event_type == "response.completed":
            response = payload.get("response")
            if isinstance(response, dict):
                if contains_output_text_part(response.get("output")):
                    saw_output_text = True
                candidate = find_response_text(response)
                if candidate:
                    completed_text = candidate

    if failed_payload is not None:
        raise CodexLBClientError(f"Codex Responses API 返回失败事件: {failed_payload}")
    # 完成事件提供最终全文；增量事件可能缺失开头片段，不能优先用于最终结果。
    text = (completed_text or "").strip() or "".join(parts).strip()
    if text:
        return text
    # 空白扫描页会正常完成并明确返回空的 output_text；这与响应结构缺失 output_text 是两种情况。
    if saw_output_text:
        return ""
    raise CodexLBClientError("Codex Responses API 流中未找到 output_text。")


def summarize_event_stream(stream_text: str) -> dict[str, Any]:
    event_counts: dict[str, int] = {}
    response_ids: list[str] = []
    malformed_json_events = 0
    data_events = 0
    done_markers = 0
    terminal_event: str | None = None
    last_event_type: str | None = None

    for block in iter_sse_blocks(stream_text):
        event_name, data_text = parse_sse_block(block)
        if not data_text:
            continue
        if data_text == "[DONE]":
            done_markers += 1
            last_event_type = event_name or "[DONE]"
            continue
        data_events += 1
        try:
            payload = json.loads(data_text)
        except json.JSONDecodeError:
            malformed_json_events += 1
            event_type = event_name or "malformed_json"
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            last_event_type = event_type
            continue
        if not isinstance(payload, dict):
            event_type = event_name or "non_object_json"
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            last_event_type = event_type
            continue

        event_type = str(payload.get("type") or event_name or "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        last_event_type = event_type
        if event_type in {"response.completed", "response.failed", "response.incomplete"}:
            terminal_event = event_type

        id_candidates = [payload.get("response_id"), payload.get("id")]
        response = payload.get("response")
        if isinstance(response, dict):
            id_candidates.append(response.get("id"))
        for candidate in id_candidates:
            if isinstance(candidate, str) and candidate and candidate not in response_ids:
                response_ids.append(candidate)

    return {
        "schema_version": 1,
        "recorded_at": trace_now_iso(),
        "stream_chars": len(stream_text),
        "stream_utf8_bytes": len(stream_text.encode("utf-8")),
        "sse_blocks": len(iter_sse_blocks(stream_text)),
        "data_events": data_events,
        "event_counts": event_counts,
        "last_event_type": last_event_type,
        "terminal_event": terminal_event,
        "done_markers": done_markers,
        "malformed_json_events": malformed_json_events,
        "response_ids": response_ids,
    }


def iter_sse_blocks(stream_text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in stream_text.splitlines():
        line = raw_line.rstrip("\r")
        if line:
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def parse_sse_block(lines: list[str]) -> tuple[str | None, str]:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    return event_name, "\n".join(data_lines).strip()


def collect_text_parts(content: Any, parts: list[str]) -> None:
    if isinstance(content, str):
        parts.append(content)
        return
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            parts.append(text)
        return
    if isinstance(content, list):
        for item in content:
            collect_text_parts(item, parts)


def contains_output_text_part(content: Any) -> bool:
    if isinstance(content, dict):
        if content.get("type") == "output_text" and isinstance(content.get("text"), str):
            return True
        return contains_output_text_part(content.get("content"))
    if isinstance(content, list):
        return any(contains_output_text_part(item) for item in content)
    return False

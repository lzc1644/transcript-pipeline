from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


PDF_OCR_PROTOCOL_VERSION = "codex-api-image-page-v1"
PDF_OCR_RETRY_FALLBACK_MODEL = "gemini-3.5-flash-lite"
PDF_OCR_RETRY_FALLBACK_REASONING_EFFORT = "medium"


def is_upstream_ocr_output_error(error: str) -> bool:
    """仅识别明确的输出拦截/截断，不把超时、TLS、限流等错误当作切模依据。"""
    normalized = error.casefold()
    return any(marker in normalized for marker in (
        "response.incomplete",
        "content_filter",
        "safety_violation",
        "regurg_plugin",
        "regurg plugin blocked the output stream",
        "响应流未完整结束",
    ))


@dataclass(frozen=True)
class PDFOCRRunIdentity:
    """唯一标识一份 PDF 在一组 OCR 配置下的页检查点。"""

    source_digest: str
    model: str
    reasoning_effort: str
    protocol_version: str = PDF_OCR_PROTOCOL_VERSION

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "source_digest": self.source_digest,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "protocol_version": self.protocol_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "source_digest": self.source_digest,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "protocol_version": self.protocol_version,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class PDFOCRPageModel:
    model: str
    reasoning_effort: str
    fast_mode: bool = False


@dataclass(frozen=True)
class PDFOCRRetryPlan:
    """父身份不变，只为指定物理页选择补页模型。"""

    identity_fingerprint: str
    page_numbers: tuple[int, ...]

    def to_dict(self) -> dict:
        return {
            "identity_fingerprint": self.identity_fingerprint,
            "page_numbers": list(self.page_numbers),
        }

    @classmethod
    def from_dict(cls, value: object) -> PDFOCRRetryPlan | None:
        if not isinstance(value, dict) or not isinstance(value.get("identity_fingerprint"), str):
            return None
        pages = value.get("page_numbers")
        if not isinstance(pages, list):
            return None
        return cls(value["identity_fingerprint"], tuple(sorted({
            page for page in pages if type(page) is int and page > 0
        })))


def build_pdf_ocr_retry_plan(
    identity: PDFOCRRunIdentity,
    page_errors: dict[int, str],
    previous_plan: PDFOCRRetryPlan | None = None,
) -> PDFOCRRetryPlan:
    """纯规划：仅 GPT 的输出失败页切模，同身份已有补页计划持续保留。"""
    pages: set[int] = set()
    if identity.model.casefold().startswith("gpt"):
        if previous_plan is not None and previous_plan.identity_fingerprint == identity.fingerprint:
            pages.update(previous_plan.page_numbers)
        pages.update(page for page, error in page_errors.items()
                     if page > 0 and is_upstream_ocr_output_error(error))
    return PDFOCRRetryPlan(identity.fingerprint, tuple(sorted(pages)))


def select_pdf_ocr_page_model(
    primary: PDFOCRPageModel,
    page_number: int,
    retry_plan: PDFOCRRetryPlan | None = None,
) -> PDFOCRPageModel:
    if (retry_plan is not None and primary.model.casefold().startswith("gpt")
            and page_number in retry_plan.page_numbers):
        return PDFOCRPageModel(PDF_OCR_RETRY_FALLBACK_MODEL, PDF_OCR_RETRY_FALLBACK_REASONING_EFFORT)
    return primary


@dataclass(frozen=True)
class PDFOCRPageState:
    """统一描述一次 PDF OCR 当前已完成页和失败页。"""

    page_count: int
    completed_page_numbers: tuple[int, ...] = ()
    page_errors: dict[int, str] = field(default_factory=dict)
    fallback_page_models: dict[int, str] = field(default_factory=dict)
    status: str = "running"

    @property
    def completed_pages(self) -> int:
        return len(self.completed_page_numbers)

    @property
    def failed_page_numbers(self) -> tuple[int, ...]:
        return tuple(sorted(self.page_errors))

    @property
    def resumable(self) -> bool:
        return self.completed_pages < self.page_count


def calculate_file_sha256(source_path: Path) -> str:
    """流式计算文件内容指纹，避免大 PDF 被一次性读入内存。"""
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OSError(f"无法计算 PDF 内容指纹: {source_path} | {exc}") from exc
    return digest.hexdigest()


def build_pdf_ocr_run_identity(source_pdf: Path, model: str, reasoning_effort: str = "") -> PDFOCRRunIdentity:
    return PDFOCRRunIdentity(
        source_digest=calculate_file_sha256(source_pdf),
        model=model.strip(),
        reasoning_effort=reasoning_effort.strip(),
    )


def build_pdf_ocr_checkpoint_namespace(
    checkpoint_root: Path,
    identity: PDFOCRRunIdentity,
) -> Path:
    return checkpoint_root.expanduser().resolve() / identity.fingerprint

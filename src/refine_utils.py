from __future__ import annotations

import math
import json
import logging
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from src.codex_lb_client import CodexLBClient, CodexLBClientError, endpoint_url
from src.ocr_page_markers import strip_ocr_page_break_markers
from src.refine_inputs import DualAsrInput, DualAsrInputError, load_dual_asr_input
from src.request_trace import (
    TRACE_SCHEMA_VERSION,
    RequestTrace,
    build_refine_request_trace,
    create_trace_run_id,
    describe_refine_attempt,
    endpoint_metadata,
    exception_metadata,
    sanitized_proxy_environment,
    text_fingerprint,
    trace_now_iso,
    update_refine_diagnostics_summary,
)
from src.runtime_utils import ensure_directory, relativize_path
from src.schemas import LoadedSettings

PROMPT_MISSING_ERROR = "缺少阶段 6 提示词配置，请检查 prompts.classify_and_correct"
BACKEND_CODEX_API = "codex_api"
BACKEND_CODEX = "codex_cli"
BACKEND_AGY = "agy"
BACKEND_FALLBACK = "local_fallback"
LEGACY_DUAL_REFINEMENT_BACKENDS = (BACKEND_CODEX, BACKEND_AGY)
VALID_REFINEMENT_BACKENDS = (BACKEND_CODEX_API, BACKEND_AGY, BACKEND_CODEX)
TARGET_MINIMAL_EDIT_BLOCKS = 20
REFINE_JSON_CONTRACT = "请只返回 JSON，字段必须包含：final_markdown、section_map、refinement_notes、needs_review_sections、deletion_candidates。"


class RefinementError(RuntimeError):
    """Raised when refinement fails."""


class RefinementInputEmptyError(RefinementError):
    """Raised when there are no classified files to refine."""


class PromptLoadError(RefinementError):
    """Raised when prompt files cannot be loaded."""


class CLIBackendError(RefinementError):
    """Raised when an AI backend fails."""


class CLIBackendRetryableError(CLIBackendError):
    """Raised when an AI backend fails in a retryable way."""


class RefinementOutputValidationError(RefinementError):
    """Raised when a refinement result still violates the final Markdown contract after retry."""


@dataclass(frozen=True)
class RefinementInputPaths:
    basename: str
    asr_text_path: Path
    reference_text_path: Path | None
    dual_asr_input: DualAsrInput | None = None


@dataclass(frozen=True)
class RefinementBlock:
    index: int
    current_text: str
    previous_anchor: str
    next_anchor: str


@dataclass(frozen=True)
class MinimalEditBlockResult:
    block: RefinementBlock
    edited_text: str
    deletion_candidates: list[dict[str, Any]]
    edit_notes: list[str]
    needs_review_sections: list[dict[str, Any]]
    failed_reason: str | None = None


@dataclass(frozen=True)
class PreReplacementSegment:
    segment_type: str
    text: str
    source_text: str
    reference_text: str
    start_sentence_index: int
    end_sentence_index: int


@dataclass(frozen=True)
class BackendDocumentRefinementResult:
    backend: str
    model_name: str
    final_markdown: str
    refinement_strategy: str
    refinement_reason: str
    needs_review_sections: list[dict[str, Any]]
    refinement_notes: list[str]
    edited_plain_text: str = ""
    edit_operations: list[str] = field(default_factory=list)
    deletion_candidates: list[dict[str, Any]] = field(default_factory=list)
    fidelity_report: dict[str, Any] = field(default_factory=dict)
    section_map: list[dict[str, Any]] = field(default_factory=list)
    backend_statuses: dict[str, str] = field(default_factory=dict)
    validation_retry_count: int = 0
    validation_failure_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RefinementOutputPath:
    json_path: Path


@dataclass(frozen=True)
class RefinementBatchItem:
    basename: str
    output_path: Path | None
    success: bool
    skipped: bool
    selected_backends: list[str]
    reason: str | None = None


@dataclass(frozen=True)
class RefinementBatchSummary:
    total: int
    success: int
    skipped: int
    failed: int
    backends: list[str]
    items: list[RefinementBatchItem]


def iter_asr_text_files(asr_dir: Path) -> list[Path]:
    if not asr_dir.exists():
        return []
    return sorted(path for path in asr_dir.iterdir() if path.is_file() and path.suffix.lower() == ".txt")


def build_refinement_output_path(asr_text_path: Path, output_dir: Path) -> RefinementOutputPath:
    return RefinementOutputPath(json_path=output_dir / f"{asr_text_path.stem}.json")


def build_backend_output_json_path(output_path: RefinementOutputPath, backend: str) -> Path:
    return output_path.json_path.with_name(f"{output_path.json_path.stem}.{backend}.json")


def resolve_requested_backends(requested_backend: str | None, configured_backends: list[str]) -> list[str]:
    if requested_backend == "both":
        return list(LEGACY_DUAL_REFINEMENT_BACKENDS)

    if requested_backend:
        if requested_backend not in VALID_REFINEMENT_BACKENDS:
            raise RefinementError(f"未知阶段 6 后端选择: {requested_backend}")
        return [requested_backend]

    resolved: list[str] = []
    for backend in configured_backends:
        if backend not in VALID_REFINEMENT_BACKENDS:
            raise RefinementError(f"配置中存在未知阶段 6 后端: {backend}")
        if backend not in resolved:
            resolved.append(backend)
    return resolved


def normalize_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_multiline_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        if not line:
            if buffer:
                paragraphs.append(" ".join(buffer).strip())
                buffer = []
            continue
        buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer).strip())
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph).strip()


def markdown_to_plain_text(markdown_text: str, *, preserve_headings: bool = False) -> str:
    lines: list[str] = []
    in_code_block = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped.startswith("#"):
            if not preserve_headings:
                continue
            stripped = re.sub(r"^#{1,6}\s+", "", stripped)
        if stripped.startswith(">"):
            stripped = stripped.lstrip(">").strip()
        if not stripped:
            lines.append("")
            continue
        lines.append(stripped)
    return normalize_multiline_text("\n".join(lines))


def truncate_for_prompt(text: str, max_chars: int) -> str:
    normalized = normalize_inline_text(text)
    if max_chars <= 0 or len(normalized) <= max_chars:
        return normalized
    if max_chars <= 1:
        return normalized[:max_chars]
    return f"{normalized[: max_chars - 1]}…"


def strip_markdown_fence(text: str) -> str:
    fenced = text.strip()
    if fenced.startswith("```"):
        fenced = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", fenced)
        fenced = re.sub(r"\n?```$", "", fenced)
    return fenced.strip()


def extract_json_payload(text: str) -> dict[str, Any]:
    candidate = strip_markdown_fence(text)
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise CLIBackendError("CLI 输出中未找到可解析的 JSON 对象。")

    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CLIBackendError(f"CLI 输出中的 JSON 解析失败: {exc}") from exc

    if not isinstance(parsed, dict):
        raise CLIBackendError("CLI 输出的 JSON 顶层必须是对象。")
    return parsed


def load_refinement_prompt(loaded_settings: LoadedSettings) -> str:
    prompts = loaded_settings.settings.prompts
    if prompts is None or not prompts.classify_and_correct:
        raise PromptLoadError(PROMPT_MISSING_ERROR)

    prompt_path = loaded_settings.resolve_path(prompts.classify_and_correct)
    if not prompt_path.exists():
        raise PromptLoadError(f"阶段 6 提示词文件不存在: {prompt_path}")

    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptLoadError(f"无法读取阶段 6 提示词文件: {prompt_path} | {exc}") from exc


def resolve_refinement_input_paths(loaded_settings: LoadedSettings, asr_text_path: Path) -> RefinementInputPaths:
    basename = asr_text_path.stem
    asr_text_path = loaded_settings.path_for("asr_dir") / f"{basename}.txt"
    reference_text_path = loaded_settings.path_for("extracted_text_dir") / f"{basename}.txt"
    reference_required = loaded_settings.settings.reference.enabled

    if not asr_text_path.exists():
        raise RefinementError(f"阶段 6 缺少对应 ASR 文本文件: {asr_text_path}")
    if reference_required and not reference_text_path.exists():
        raise RefinementError(f"阶段 6 缺少对应参考原文文件: {reference_text_path}")

    try:
        dual_input = load_dual_asr_input(loaded_settings, asr_text_path)
    except DualAsrInputError as exc:
        raise RefinementError(str(exc)) from exc
    return RefinementInputPaths(
        basename=basename,
        asr_text_path=asr_text_path,
        reference_text_path=reference_text_path if reference_required else None,
        dual_asr_input=dual_input,
    )


def asr_input_metadata(input_paths: RefinementInputPaths, loaded_settings: LoadedSettings) -> dict[str, Any]:
    if input_paths.dual_asr_input is None:
        return {}
    return input_paths.dual_asr_input.provenance(loaded_settings.project_root)


def load_text_file(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RefinementError(f"无法读取{label}文件: {path} | {exc}") from exc

    if not text:
        raise RefinementError(f"{label}文件为空: {path}")
    return text


def split_text_into_paragraphs(text: str) -> list[str]:
    single_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(single_lines) > 1 and not re.search(r"\n\s*\n", text):
        return single_lines
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    return paragraphs or single_lines


def split_text_into_sentences(text: str) -> list[str]:
    units: list[str] = []
    for paragraph in split_text_into_paragraphs(text):
        parts = re.split(r"(?<=[。！？!?；;])", paragraph)
        normalized_parts = [normalize_inline_text(part) for part in parts if normalize_inline_text(part)]
        if normalized_parts:
            units.extend(normalized_parts)
        elif paragraph.strip():
            units.append(normalize_inline_text(paragraph))
    return units


def normalize_for_match(text: str) -> str:
    return re.sub(r"[\W_]+", "", normalize_inline_text(text)).lower()


def contains_any_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def compute_extra_content_ratio(source_text: str, reference_text: str) -> float:
    source_length = len(normalize_for_match(source_text))
    reference_length = len(normalize_for_match(reference_text))
    if source_length <= 0:
        return 0.0
    return round(max(0, source_length - reference_length) / source_length, 4)


def get_safe_replace_settings(loaded_settings: LoadedSettings) -> dict[str, float]:
    llm_settings = loaded_settings.settings.llm
    return {
        "min_score": float(getattr(llm_settings, "safe_replace_min_score", 88.0)),
        "min_margin": float(getattr(llm_settings, "safe_replace_min_margin", 6.0)),
        "length_ratio_min": float(getattr(llm_settings, "safe_replace_length_ratio_min", 0.8)),
        "length_ratio_max": float(getattr(llm_settings, "safe_replace_length_ratio_max", 1.2)),
        "max_extra_content_ratio": float(getattr(llm_settings, "safe_replace_max_extra_content_ratio", 0.12)),
        "min_run_length": max(1, int(getattr(llm_settings, "safe_replace_min_run_length", 2))),
    }


def find_best_reference_match(sentence_text: str, reference_sentences: list[str]) -> tuple[int | None, str, float, float]:
    normalized_source = normalize_for_match(sentence_text)
    if not normalized_source or not reference_sentences:
        return None, "", 0.0, 0.0

    scored = [
        (index, reference_sentence, float(fuzz.ratio(normalized_source, normalize_for_match(reference_sentence))))
        for index, reference_sentence in enumerate(reference_sentences)
        if normalize_for_match(reference_sentence)
    ]
    if not scored:
        return None, "", 0.0, 0.0
    scored.sort(key=lambda item: item[2], reverse=True)
    best_index, best_text, best_score = scored[0]
    second_score = scored[1][2] if len(scored) > 1 else 0.0
    return best_index, best_text, best_score, round(best_score - second_score, 2)


def is_safe_replace_candidate(
    sentence_text: str,
    reference_text: str,
    *,
    best_score: float,
    margin: float,
    loaded_settings: LoadedSettings,
) -> bool:
    settings = get_safe_replace_settings(loaded_settings)
    classification_settings = loaded_settings.settings.classification
    normalized_source = normalize_for_match(sentence_text)
    normalized_reference = normalize_for_match(reference_text)
    if not normalized_source or not normalized_reference:
        return False
    if contains_any_keyword(sentence_text, classification_settings.lecture_markers):
        return False
    if contains_any_keyword(sentence_text, classification_settings.qa_keywords):
        return False
    if contains_any_keyword(sentence_text, classification_settings.intro_keywords):
        return False

    source_length = len(normalized_source)
    reference_length = len(normalized_reference)
    length_ratio = round(source_length / max(reference_length, 1), 4)
    extra_content_ratio = compute_extra_content_ratio(sentence_text, reference_text)
    return (
        best_score >= settings["min_score"]
        and margin >= settings["min_margin"]
        and settings["length_ratio_min"] <= length_ratio <= settings["length_ratio_max"]
        and extra_content_ratio <= settings["max_extra_content_ratio"]
    )


def build_pre_replaced_document(
    *,
    asr_full_text: str,
    reference_full_text: str,
    loaded_settings: LoadedSettings,
) -> list[PreReplacementSegment]:
    source_sentences = split_text_into_sentences(asr_full_text)
    # 只过滤规则预替换的匹配输入；交给模型的全文附件仍保留页边界。
    reference_sentences = split_text_into_sentences(strip_ocr_page_break_markers(reference_full_text))
    settings = get_safe_replace_settings(loaded_settings)

    sentence_states: list[dict[str, Any]] = []
    for index, source_sentence in enumerate(source_sentences):
        ref_index, ref_text, best_score, margin = find_best_reference_match(source_sentence, reference_sentences)
        sentence_states.append(
            {
                "index": index,
                "source_text": source_sentence,
                "reference_text": ref_text,
                "reference_index": ref_index,
                "safe_candidate": ref_index is not None
                and is_safe_replace_candidate(
                    source_sentence,
                    ref_text,
                    best_score=best_score,
                    margin=margin,
                    loaded_settings=loaded_settings,
                ),
            }
        )

    locked_ranges: list[tuple[int, int]] = []
    start: int | None = None
    last_reference_index: int | None = None
    for state in sentence_states:
        current_reference_index = state["reference_index"]
        is_continuation = (
            start is not None
            and state["safe_candidate"]
            and last_reference_index is not None
            and current_reference_index is not None
            and current_reference_index == last_reference_index + 1
        )
        if start is None and state["safe_candidate"]:
            start = state["index"]
            last_reference_index = current_reference_index
            continue
        if is_continuation:
            last_reference_index = current_reference_index
            continue
        if start is not None and last_reference_index is not None:
            end = sentence_states[state["index"] - 1]["index"]
            if end - start + 1 >= settings["min_run_length"]:
                locked_ranges.append((start, end))
        start = state["index"] if state["safe_candidate"] else None
        last_reference_index = current_reference_index if state["safe_candidate"] else None
    if start is not None and last_reference_index is not None:
        end = sentence_states[-1]["index"]
        if end - start + 1 >= settings["min_run_length"]:
            locked_ranges.append((start, end))

    locked_sentence_indexes = {
        sentence_index
        for range_start, range_end in locked_ranges
        for sentence_index in range(range_start, range_end + 1)
    }

    segments: list[PreReplacementSegment] = []
    current_type: str | None = None
    current_source: list[str] = []
    current_text: list[str] = []
    current_reference: list[str] = []
    segment_start = 0

    def flush_segment(segment_end: int) -> None:
        nonlocal current_type, current_source, current_text, current_reference, segment_start
        if current_type is None or not current_text:
            return
        if current_type == "unlocked_text":
            chunk_size = resolve_chunk_paragraphs(
                len(current_text),
                loaded_settings.settings.llm.block_batch_size,
            )
            for offset in range(0, len(current_text), chunk_size):
                chunk_text = current_text[offset : offset + chunk_size]
                chunk_source = current_source[offset : offset + chunk_size]
                segments.append(
                    PreReplacementSegment(
                        segment_type=current_type,
                        text="\n\n".join(chunk_text).strip(),
                        source_text="\n\n".join(chunk_source).strip(),
                        reference_text="",
                        start_sentence_index=segment_start + offset,
                        end_sentence_index=min(segment_start + offset + len(chunk_text) - 1, segment_end),
                    )
                )
        else:
            segments.append(
                PreReplacementSegment(
                    segment_type=current_type,
                    text="\n\n".join(current_text).strip(),
                    source_text="\n\n".join(current_source).strip(),
                    reference_text="".join(current_reference).strip(),
                    start_sentence_index=segment_start,
                    end_sentence_index=segment_end,
                )
            )
        current_type = None
        current_source = []
        current_text = []
        current_reference = []

    for state in sentence_states:
        segment_type = "locked_quote" if state["index"] in locked_sentence_indexes else "unlocked_text"
        segment_text = state["reference_text"] if segment_type == "locked_quote" else state["source_text"]
        segment_reference_text = state["reference_text"] if segment_type == "locked_quote" else ""
        if current_type != segment_type:
            flush_segment(state["index"] - 1)
            current_type = segment_type
            segment_start = state["index"]
        current_source.append(state["source_text"])
        current_text.append(segment_text)
        if segment_reference_text:
            current_reference.append(segment_reference_text)
    flush_segment(len(sentence_states) - 1)
    return segments


def resolve_chunk_paragraphs(paragraph_count: int, configured_chunk_paragraphs: int) -> int:
    base_chunk_size = max(1, configured_chunk_paragraphs)
    if paragraph_count <= TARGET_MINIMAL_EDIT_BLOCKS:
        return base_chunk_size
    return max(base_chunk_size, math.ceil(paragraph_count / TARGET_MINIMAL_EDIT_BLOCKS))


def build_refinement_blocks(
    text: str,
    *,
    chunk_paragraphs: int,
    anchor_paragraphs: int,
) -> list[RefinementBlock]:
    paragraphs = split_text_into_paragraphs(text)
    if not paragraphs:
        return []

    chunk_size = resolve_chunk_paragraphs(len(paragraphs), chunk_paragraphs)
    anchor_size = max(0, anchor_paragraphs)
    blocks: list[RefinementBlock] = []
    for block_index, start in enumerate(range(0, len(paragraphs), chunk_size)):
        current_chunk = paragraphs[start : start + chunk_size]
        previous_chunk = paragraphs[max(0, start - anchor_size) : start]
        next_chunk = paragraphs[start + chunk_size : start + chunk_size + anchor_size]
        blocks.append(
            RefinementBlock(
                index=block_index,
                current_text="\n\n".join(current_chunk).strip(),
                previous_anchor="\n\n".join(previous_chunk).strip(),
                next_anchor="\n\n".join(next_chunk).strip(),
            )
        )
    return blocks


def build_fulltext_refine_prompt(
    prompt_text: str,
    input_paths: RefinementInputPaths,
    *,
    asr_full_text: str,
    reference_full_text: str,
) -> str:
    sections = [
        prompt_text.strip(),
        "",
        f"当前文件: {input_paths.basename}.txt",
        "下面提供录音转写文本和参考原文，请直接按要求输出最终 Markdown 的 JSON 结果。",
    ]
    sections.extend(
        [
            "",
            "录音转写文本：",
            asr_full_text,
            "",
            "参考原文：",
            reference_full_text,
        ]
    )
    return "\n".join(sections).strip()


def load_markdown_assemble_prompt(loaded_settings: LoadedSettings, *, content_type: str | None = None) -> str:
    prompts = loaded_settings.settings.prompts
    prompt_field = "conversation_cleanup" if (content_type or "").strip() == "conversation" else "final_cleanup"
    prompt_value = getattr(prompts, prompt_field, None) if prompts is not None else None
    if prompts is None or not prompt_value:
        raise PromptLoadError(f"缺少阶段 6 Markdown 组装提示词配置，请检查 prompts.{prompt_field}")

    prompt_path = loaded_settings.resolve_path(prompt_value)
    if not prompt_path.exists():
        raise PromptLoadError(f"阶段 6 Markdown 组装提示词文件不存在: {prompt_path}")

    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptLoadError(f"无法读取阶段 6 Markdown 组装提示词文件: {prompt_path} | {exc}") from exc


def build_minimal_edit_prompt(
    prompt_text: str,
    input_paths: RefinementInputPaths,
    block: RefinementBlock,
    *,
    reference_full_text: str,
) -> str:
    sections = [
        prompt_text.strip(),
        "",
        "你现在只允许编辑“当前块”文本，不得改写前后文锚点。",
        "你的任务仅限于：",
        "1. 添加标点与断句。",
        "2. 修正明显错字、别字、同音误识别。",
        "3. 统一为简体中文。",
        "4. 删除你判断为无意义的口语噪音，但删除噪音必须记录。",
        "",
        "绝对禁止：",
        "1. 不得总结压缩。",
        "2. 不得把多句合并成抽象概括句。",
        "3. 不得改写讲话风格为书面总结。",
        "4. 不得增加原文没有表达出的信息。",
        "5. 无法确认时保留 ASR 原文，不得猜测。",
        "",
        "请只返回 JSON，字段必须包含：edited_text、deletion_candidates、edit_notes、needs_review_sections。",
        "",
        f"当前文件: {input_paths.basename}.txt",
        f"当前块序号: {block.index}",
        "",
        "前文锚点：",
        block.previous_anchor or "（无）",
        "",
        "当前块正文：",
        block.current_text,
        "",
        "后文锚点：",
        block.next_anchor or "（无）",
        "",
        "参考原文：",
        reference_full_text or "（无）",
    ]
    return "\n".join(sections).strip()


def build_markdown_assemble_prompt(
    prompt_text: str,
    input_paths: RefinementInputPaths,
    *,
    edited_plain_text: str,
    reference_full_text: str,
) -> str:
    sections = [
        prompt_text.strip(),
        "",
        "你现在只负责 Markdown 结构整理。",
        "你的任务仅限于：识别引用块、普通讲解段落和提问环节，并输出最终 Markdown。",
        "不得改写 edited_plain_text 的措辞，不得总结，不得压缩，不得新增说明。",
        "若无法判断结构，保留原顺序和原文措辞。",
        "请只返回 JSON，字段必须包含：final_markdown、section_map、refinement_notes、needs_review_sections。",
        "",
        f"当前文件: {input_paths.basename}.txt",
        "",
        "edited_plain_text：",
        edited_plain_text,
        "",
        "参考原文：",
        reference_full_text or "（无）",
    ]
    return "\n".join(sections).strip()


def build_pre_replaced_plain_text(pre_replaced_segments: list[PreReplacementSegment]) -> str:
    return "\n\n".join(segment.text for segment in pre_replaced_segments if segment.text.strip()).strip()


def build_single_pass_refine_prompt(
    prompt_text: str,
    input_paths: RefinementInputPaths,
    *,
    backend: str,
    pre_replaced_segments: list[PreReplacementSegment],
    reference_full_text: str,
) -> str:
    # 业务编辑政策仅由传入的 Markdown 提示词拥有；这里只装配输入协议和接口契约。
    has_reference = input_paths.reference_text_path is not None
    sections = [prompt_text.strip(), REFINE_JSON_CONTRACT, f"当前文件: {input_paths.basename}.txt"]
    if input_paths.dual_asr_input is not None:
        sections.extend([
            "输入模式：同源双 ASR 联合校对。A/B 来自同一音频，没有提供音频本身。",
            "WINDOW 是按段落起点归入的共同分钟窗，仅作粗定位，不是精确字级对齐或说话人边界。",
            "双 ASR 原始全文：",
            input_paths.dual_asr_input.render(),
        ])
    else:
        rendered_segments = []
        for index, segment in enumerate(pre_replaced_segments, start=1):
            label = f"[SEGMENT {index:02d}][{segment.segment_type}]" if has_reference else f"[SEGMENT {index:02d}]"
            rendered_segments.extend([label, segment.text, ""])
        if has_reference:
            sections.extend([
                "输入模式：整篇单次校对。预替换全文是主输入；整篇参考原文只是校正附件。",
                "工程契约：不得改写 locked_quote 的实词内容，只允许调整标点、断句和引用格式。",
                "预替换全文：",
            ])
        else:
            sections.extend([
                "输入模式：对谈录屏保真转录整理。录音转写全文是唯一主输入，没有参考原文或参考书目。",
                "录音转写全文：",
            ])
        sections.append("\n".join(rendered_segments).strip())
    if has_reference:
        sections.extend(["整篇参考原文：", reference_full_text or "（无）"])
    else:
        sections.append("本任务没有参考附件。")
    return "\n\n".join(sections).strip()


def locked_quotes_preserved(final_markdown: str, pre_replaced_segments: list[PreReplacementSegment]) -> bool:
    # 标题也是锁定原文的一部分；只去 Markdown 标记，不能丢弃标题文字。
    plain_text = normalize_for_match(markdown_to_plain_text(final_markdown, preserve_headings=True))
    for segment in pre_replaced_segments:
        if segment.segment_type != "locked_quote":
            continue
        if normalize_for_match(segment.text) not in plain_text:
            return False
    return True


def normalize_deletion_candidates(raw_candidates: Any) -> list[dict[str, str]]:
    deletion_candidates: list[dict[str, str]] = []
    if not isinstance(raw_candidates, list):
        return deletion_candidates
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        source_excerpt = normalize_inline_text(str(item.get("source_excerpt", "")).strip())
        deleted_text = normalize_inline_text(str(item.get("deleted_text", "")).strip())
        reason = normalize_inline_text(str(item.get("reason", "")).strip())
        if not source_excerpt and not deleted_text and not reason:
            continue
        deletion_candidates.append(
            {
                "source_excerpt": source_excerpt,
                "deleted_text": deleted_text,
                "reason": reason or "model_deleted_noise",
            }
        )
    return deletion_candidates


def validate_minimal_edit_result(
    *,
    source_text: str,
    edited_text: str,
    deletion_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    source_inline = normalize_inline_text(source_text)
    edited_inline = normalize_inline_text(edited_text)
    deleted_chars = sum(len(normalize_inline_text(str(item.get("deleted_text", "")))) for item in deletion_candidates)
    allowed_removed_chars = deleted_chars + 8
    length_ratio = round(len(edited_inline) / len(source_inline), 4) if source_inline else 1.0
    removed_chars = max(0, len(source_inline) - len(edited_inline))
    reasons: list[str] = []

    if source_inline and edited_inline and removed_chars > allowed_removed_chars and length_ratio < 0.85:
        reasons.append("length_ratio_too_low")

    summary_phrases = [
        "主要是",
        "表达了",
        "作者想说明",
        "总体来说",
        "这段的意思是",
        "主要讲的是",
        "这里是在说",
    ]
    if any(phrase in edited_inline for phrase in summary_phrases):
        reasons.append("summary_phrase_detected")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "length_ratio": length_ratio,
        "removed_chars": removed_chars,
        "deletion_count": len(deletion_candidates),
    }


def run_subprocess(command: list[str], *, prompt: str, cwd: Path, timeout_seconds: int) -> str:
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(cwd),
            timeout=timeout_seconds,
            check=False,
        )
    except OSError as exc:
        raise CLIBackendError(f"CLI 命令启动失败: {' '.join(command)} | {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CLIBackendError(f"CLI 命令执行超时: {' '.join(command)}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        error_message = f"CLI 命令执行失败: {' '.join(command)} | {stderr}"
        if is_gemini_capacity_error(stderr):
            raise CLIBackendRetryableError(error_message)
        raise CLIBackendError(error_message)

    output = completed.stdout.strip()
    if not output:
        raise CLIBackendError(f"CLI 命令没有返回内容: {' '.join(command)}")
    return output


def is_gemini_capacity_error(text: str) -> bool:
    normalized = text.upper()
    return "429" in normalized or "MODEL_CAPACITY_EXHAUSTED" in normalized or "RESOURCE_EXHAUSTED" in normalized


def parse_backend_document_result(backend: str, payload: dict[str, Any]) -> BackendDocumentRefinementResult:
    final_markdown = str(payload.get("final_markdown", "")).strip()
    if not final_markdown:
        # 向后兼容旧结果，避免单个后端仍返回旧字段时整批失败。
        legacy_full_text = normalize_multiline_text(str(payload.get("refined_full_text", "")).strip())
        if legacy_full_text:
            final_markdown = legacy_full_text
    if not final_markdown:
        raise CLIBackendError("CLI 返回结构无效：缺少 final_markdown。")
    plain_text = markdown_to_plain_text(final_markdown)

    raw_review_sections = payload.get("needs_review_sections", [])
    needs_review_sections: list[dict[str, Any]] = []
    if isinstance(raw_review_sections, list):
        for item in raw_review_sections:
            if not isinstance(item, dict):
                continue
            excerpt = normalize_inline_text(str(item.get("excerpt", "")).strip())
            reason = normalize_inline_text(str(item.get("reason", "")).strip())
            if not excerpt and not reason:
                continue
            needs_review_sections.append(
                {
                    "excerpt": excerpt or truncate_for_prompt(plain_text, 80),
                    "reason": reason or f"{backend}_needs_review",
                }
            )

    raw_notes = payload.get("refinement_notes", [])
    refinement_notes = [
        normalize_inline_text(str(item).strip())
        for item in raw_notes
        if normalize_inline_text(str(item).strip())
    ] if isinstance(raw_notes, list) else []

    return BackendDocumentRefinementResult(
        backend=backend,
        model_name=str(payload.get("model_name", "")).strip(),
        final_markdown=final_markdown,
        refinement_strategy=str(payload.get("refinement_strategy", f"{backend}_fulltext_refine")),
        refinement_reason=str(payload.get("refinement_reason", f"{backend}_refinement")),
        needs_review_sections=needs_review_sections,
        refinement_notes=refinement_notes,
        section_map=payload.get("section_map", []) if isinstance(payload.get("section_map", []), list) else [],
    )


def build_result_backend_status(
    result: BackendDocumentRefinementResult,
    *,
    fallback_from: str | None = None,
) -> str:
    if result.refinement_strategy == "programmatic_markdown_fallback":
        status = f"degraded_to_programmatic_fallback:{result.refinement_reason}"
    else:
        status = "returned_single_pass"
    if result.model_name:
        status = f"{status}:model={result.model_name}"
    if fallback_from:
        status = f"{status}:fallback_from={fallback_from}"
    return status


def resolve_backend_statuses(result: BackendDocumentRefinementResult) -> dict[str, str]:
    if result.backend_statuses:
        return dict(result.backend_statuses)
    return {result.backend: build_result_backend_status(result)}


def parse_minimal_edit_result(payload: dict[str, Any]) -> dict[str, Any]:
    edited_text = str(payload.get("edited_text", "")).strip()
    if not edited_text:
        raise CLIBackendError("CLI 返回结构无效：缺少 edited_text。")
    raw_notes = payload.get("edit_notes", [])
    edit_notes = [
        normalize_inline_text(str(item).strip())
        for item in raw_notes
        if normalize_inline_text(str(item).strip())
    ] if isinstance(raw_notes, list) else []
    return {
        "edited_text": edited_text,
        "deletion_candidates": normalize_deletion_candidates(payload.get("deletion_candidates", [])),
        "edit_notes": edit_notes,
        "needs_review_sections": payload.get("needs_review_sections", []) if isinstance(payload.get("needs_review_sections", []), list) else [],
    }


def run_codex_cli_payload(prompt: str, loaded_settings: LoadedSettings) -> dict[str, Any]:
    llm_settings = loaded_settings.settings.llm
    timeout_seconds = llm_settings.timeout_seconds
    configured_model = llm_settings.model.strip()
    configured_reasoning_effort = llm_settings.reasoning_effort.strip()
    with tempfile.NamedTemporaryFile("r+", encoding="utf-8", suffix=".txt", delete=True) as output_file:
        command = [
            "codex",
            "exec",
            "-C",
            str(loaded_settings.project_root),
            "-s",
            "read-only",
        ]
        if configured_model:
            command.extend(["-m", configured_model])
        if configured_reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{configured_reasoning_effort}"'])
        command.extend(["-o", output_file.name, "-"])
        run_subprocess(command, prompt=prompt, cwd=loaded_settings.project_root, timeout_seconds=timeout_seconds)
        output_file.seek(0)
        return extract_json_payload(output_file.read())


def run_codex_api_payload(
    prompt: str,
    loaded_settings: LoadedSettings,
    *,
    request_trace: RequestTrace | None = None,
) -> dict[str, Any]:
    llm_settings = loaded_settings.settings.llm
    configured_model = llm_settings.model.strip()
    if not configured_model:
        exc = CLIBackendError("codex_api 需要配置 llm.model。")
        if request_trace is not None:
            request_trace.write_json("backend-error.json", exception_metadata(exc, category="configuration"))
        raise exc

    instructions = "你是阶段 6 文稿校对助手。严格遵守用户输入中的 JSON 输出要求，只返回最终 JSON。"
    payload: dict[str, Any] = {
        "model": configured_model,
        "instructions": instructions,
        "input": prompt,
        "stream": True,
        "store": False,
    }
    configured_reasoning_effort = llm_settings.reasoning_effort.strip()
    if configured_reasoning_effort:
        payload["reasoning"] = {"effort": configured_reasoning_effort}

    client = CodexLBClient(loaded_settings.settings.codex_lb, timeout_seconds=llm_settings.timeout_seconds)
    try:
        request_endpoint = endpoint_url(client.base_url, client.settings.codex_responses_path)
    except CodexLBClientError as exc:
        if request_trace is not None:
            request_trace.write_json("backend-error.json", exception_metadata(exc, category="configuration"))
        raise CLIBackendError(f"codex_api 调用失败: {exc}") from exc

    if request_trace is not None:
        prompt_meta = text_fingerprint(prompt)
        instruction_meta = text_fingerprint(instructions)
        request_trace.write_json(
            "request-meta.json",
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "stage": "refine",
                "recorded_at": trace_now_iso(),
                "model": configured_model,
                "reasoning_effort": configured_reasoning_effort,
                "stream": True,
                "store": False,
                "timeout_seconds": llm_settings.timeout_seconds,
                "endpoint": endpoint_metadata(request_endpoint),
                "api_key_env": client.settings.api_key_env,
                "payload_fields": sorted(payload),
                "prompt_chars": prompt_meta["chars"],
                "prompt_utf8_bytes": prompt_meta["utf8_bytes"],
                "prompt_sha256": prompt_meta["sha256"],
                "instructions_chars": instruction_meta["chars"],
                "instructions_utf8_bytes": instruction_meta["utf8_bytes"],
                "instructions_sha256": instruction_meta["sha256"],
                "proxy_environment": sanitized_proxy_environment(),
            },
        )

    try:
        output_text = client.codex_responses_text(payload, request_trace=request_trace)
    except CodexLBClientError as exc:
        if request_trace is not None:
            request_trace.write_json("backend-error.json", exception_metadata(exc, category="transport_or_protocol"))
        raise CLIBackendError(f"codex_api 调用失败: {exc}") from exc
    try:
        result = extract_json_payload(output_text)
    except CLIBackendError as exc:
        if request_trace is not None:
            request_trace.write_json("parse-error.json", exception_metadata(exc, category="model_output_json"))
        raise
    if request_trace is not None:
        request_trace.write_json("parsed-output.json", result)
    result.setdefault("model_name", configured_model)
    return result


def run_codex_api(prompt: str, loaded_settings: LoadedSettings) -> BackendDocumentRefinementResult:
    payload = run_codex_api_payload(prompt, loaded_settings)
    result = parse_backend_document_result(BACKEND_CODEX_API, payload)
    return BackendDocumentRefinementResult(
        backend=result.backend,
        model_name=result.model_name or loaded_settings.settings.llm.model.strip() or BACKEND_CODEX_API,
        final_markdown=result.final_markdown,
        refinement_strategy=result.refinement_strategy,
        refinement_reason=result.refinement_reason,
        needs_review_sections=result.needs_review_sections,
        refinement_notes=result.refinement_notes,
        section_map=result.section_map,
    )


def run_codex_cli(prompt: str, loaded_settings: LoadedSettings) -> BackendDocumentRefinementResult:
    llm_settings = loaded_settings.settings.llm
    configured_model = llm_settings.model.strip()
    payload = run_codex_cli_payload(prompt, loaded_settings)
    result = parse_backend_document_result(BACKEND_CODEX, payload)
    if result.model_name:
        return result
    return BackendDocumentRefinementResult(
        backend=result.backend,
        model_name=configured_model or "codex_default",
        final_markdown=result.final_markdown,
        refinement_strategy=result.refinement_strategy,
        refinement_reason=result.refinement_reason,
        needs_review_sections=result.needs_review_sections,
        refinement_notes=result.refinement_notes,
        section_map=result.section_map,
    )


def run_agy_payload(prompt: str, loaded_settings: LoadedSettings) -> dict[str, Any]:
    llm_settings = loaded_settings.settings.llm
    models_to_try = [llm_settings.gemini_model]
    fallback_model = llm_settings.gemini_fallback_model.strip()
    if fallback_model and fallback_model not in models_to_try:
        models_to_try.append(fallback_model)

    last_error: CLIBackendError | None = None
    for index, model_name in enumerate(models_to_try):
        command = [
            "agy",
            "--model",
            model_name,
            "--print",
            prompt,
            "--print-timeout",
            f"{llm_settings.timeout_seconds}s",
        ]
        try:
            output = run_subprocess(
                command,
                prompt="",
                cwd=loaded_settings.project_root,
                timeout_seconds=llm_settings.timeout_seconds,
            )
            payload = extract_json_payload(output)
            if not str(payload.get("model_name", "")).strip():
                payload["model_name"] = model_name
            return payload
        except CLIBackendRetryableError as exc:
            last_error = exc
            if index == len(models_to_try) - 1:
                break
            continue
        except CLIBackendError as exc:
            last_error = exc
            break

    if last_error is not None:
        raise last_error
    raise CLIBackendError("agy 调用失败，未获得可用结果。")


def run_agy(prompt: str, loaded_settings: LoadedSettings) -> BackendDocumentRefinementResult:
    payload = run_agy_payload(prompt, loaded_settings)
    return parse_backend_document_result(BACKEND_AGY, payload)


def run_backend_cli(backend: str, prompt: str, loaded_settings: LoadedSettings) -> BackendDocumentRefinementResult:
    if backend == BACKEND_CODEX_API:
        return run_codex_api(prompt, loaded_settings)
    if backend == BACKEND_CODEX:
        return run_codex_cli(prompt, loaded_settings)
    if backend == BACKEND_AGY:
        return run_agy(prompt, loaded_settings)
    raise CLIBackendError(f"未知阶段 6 后端: {backend}")


def run_backend_payload(
    backend: str,
    prompt: str,
    loaded_settings: LoadedSettings,
    *,
    request_trace: RequestTrace | None = None,
) -> dict[str, Any]:
    if backend == BACKEND_CODEX_API:
        return run_codex_api_payload(prompt, loaded_settings, request_trace=request_trace)
    if backend == BACKEND_CODEX:
        return run_codex_cli_payload(prompt, loaded_settings)
    if backend == BACKEND_AGY:
        return run_agy_payload(prompt, loaded_settings)
    raise CLIBackendError(f"未知阶段 6 后端: {backend}")


def process_minimal_edit_block(
    *,
    backend: str,
    block: RefinementBlock,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    minimal_edit_prompt_text: str,
    reference_full_text: str,
) -> MinimalEditBlockResult:
    prompt = build_minimal_edit_prompt(
        minimal_edit_prompt_text,
        input_paths,
        block,
        reference_full_text=reference_full_text,
    )
    try:
        payload = run_backend_payload(backend, prompt, loaded_settings)
        parsed = parse_minimal_edit_result(payload)
    except CLIBackendError:
        return MinimalEditBlockResult(
            block=block,
            edited_text=block.current_text,
            deletion_candidates=[],
            edit_notes=[],
            needs_review_sections=[{"excerpt": truncate_for_prompt(block.current_text, 80), "reason": "block_backend_failed"}],
            failed_reason="block_backend_failed",
        )

    fidelity_report = validate_minimal_edit_result(
        source_text=block.current_text,
        edited_text=parsed["edited_text"],
        deletion_candidates=parsed["deletion_candidates"],
    )
    if not fidelity_report["passed"]:
        return MinimalEditBlockResult(
            block=block,
            edited_text=block.current_text,
            deletion_candidates=[],
            edit_notes=[],
            needs_review_sections=[{"excerpt": truncate_for_prompt(block.current_text, 80), "reason": "block_validation_failed"}],
            failed_reason="block_validation_failed",
        )

    normalized_review_sections: list[dict[str, Any]] = []
    for item in parsed["needs_review_sections"]:
        if not isinstance(item, dict):
            continue
        excerpt = normalize_inline_text(str(item.get("excerpt", "")).strip())
        reason = normalize_inline_text(str(item.get("reason", "")).strip())
        if excerpt or reason:
            normalized_review_sections.append(
                {
                    "excerpt": excerpt or truncate_for_prompt(parsed["edited_text"], 80),
                    "reason": reason or "minimal_edit_needs_review",
                }
            )

    return MinimalEditBlockResult(
        block=block,
        edited_text=parsed["edited_text"].strip(),
        deletion_candidates=parsed["deletion_candidates"],
        edit_notes=parsed["edit_notes"],
        needs_review_sections=normalized_review_sections,
    )


def build_simple_markdown(title: str, text: str) -> str:
    normalized_text = normalize_multiline_text(text)
    return f"# {title}\n\n{normalized_text}".strip()


def run_two_step_backend_refinement(
    *,
    backend: str,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    minimal_edit_prompt_text: str,
    markdown_prompt_text: str,
    asr_full_text: str,
    reference_full_text: str,
    logger: logging.Logger | None = None,
) -> BackendDocumentRefinementResult:
    llm_settings = loaded_settings.settings.llm
    blocks = build_refinement_blocks(
        asr_full_text,
        chunk_paragraphs=max(1, llm_settings.block_batch_size),
        anchor_paragraphs=1,
    )
    if not blocks:
        raise CLIBackendError(f"阶段 6 无法切分可处理文本块: {input_paths.basename}.txt")

    block_outputs: list[str] = []
    edit_operations: list[str] = []
    deletion_candidates: list[dict[str, Any]] = []
    needs_review_sections: list[dict[str, Any]] = []
    refinement_notes: list[str] = []
    model_name = llm_settings.model.strip() if backend in (BACKEND_CODEX_API, BACKEND_CODEX) else llm_settings.gemini_model

    block_concurrency = max(1, getattr(llm_settings, "block_concurrency", 1))
    block_results: dict[int, MinimalEditBlockResult] = {}
    completed_blocks = 0

    def log_block_progress(result: MinimalEditBlockResult) -> None:
        nonlocal completed_blocks
        if logger is None:
            return
        completed_blocks += 1
        status = result.failed_reason or "ok"
        logger.info(
            "阶段 6 块进度 | file=%s | backend=%s | block=%s/%s | status=%s",
            input_paths.basename,
            backend,
            completed_blocks,
            len(blocks),
            status,
        )

    if block_concurrency == 1 or len(blocks) <= 1:
        for block in blocks:
            result = process_minimal_edit_block(
                backend=backend,
                block=block,
                input_paths=input_paths,
                loaded_settings=loaded_settings,
                minimal_edit_prompt_text=minimal_edit_prompt_text,
                reference_full_text=reference_full_text,
            )
            block_results[block.index] = result
            log_block_progress(result)
    else:
        with ThreadPoolExecutor(max_workers=block_concurrency) as executor:
            futures = [
                executor.submit(
                    process_minimal_edit_block,
                    backend=backend,
                    block=block,
                    input_paths=input_paths,
                    loaded_settings=loaded_settings,
                    minimal_edit_prompt_text=minimal_edit_prompt_text,
                    reference_full_text=reference_full_text,
                )
                for block in blocks
            ]
            for future in as_completed(futures):
                result = future.result()
                block_results[result.block.index] = result
                log_block_progress(result)

    for block in blocks:
        result = block_results[block.index]
        block_outputs.append(result.edited_text)
        edit_operations.extend(result.edit_notes)
        deletion_candidates.extend(result.deletion_candidates)
        needs_review_sections.extend(result.needs_review_sections)
        if result.failed_reason:
            refinement_notes.append(result.failed_reason)

    edited_plain_text = "\n\n".join(item.strip() for item in block_outputs if item.strip()).strip()
    fidelity_report = validate_minimal_edit_result(
        source_text=asr_full_text,
        edited_text=edited_plain_text,
        deletion_candidates=deletion_candidates,
    )
    if not fidelity_report["passed"]:
        refinement_notes.append("document_validation_failed")
        edited_plain_text = normalize_multiline_text(asr_full_text)
        deletion_candidates = []
        fidelity_report = {
            "passed": True,
            "reasons": ["fallback_to_original_asr_after_document_validation"],
        }

    assemble_prompt = build_markdown_assemble_prompt(
        markdown_prompt_text,
        input_paths,
        edited_plain_text=edited_plain_text,
        reference_full_text=reference_full_text,
    )
    try:
        assemble_payload = run_backend_payload(backend, assemble_prompt, loaded_settings)
        document_result = parse_backend_document_result(backend, assemble_payload)
        model_name = document_result.model_name or str(assemble_payload.get("model_name", "")).strip() or model_name
    except CLIBackendError:
        refinement_notes.append("markdown_assemble_failed_use_programmatic_fallback")
        document_result = BackendDocumentRefinementResult(
            backend=backend,
            model_name=model_name or backend,
            final_markdown=build_simple_markdown(input_paths.basename, edited_plain_text),
            refinement_strategy="programmatic_markdown_fallback",
            refinement_reason="markdown_assemble_failed",
            needs_review_sections=[],
            refinement_notes=[],
        )

    markdown_fidelity = validate_minimal_edit_result(
        source_text=edited_plain_text,
        edited_text=markdown_to_plain_text(document_result.final_markdown),
        deletion_candidates=[],
    )
    final_markdown = document_result.final_markdown
    if not markdown_fidelity["passed"]:
        refinement_notes.append("markdown_content_changed_use_programmatic_fallback")
        final_markdown = build_simple_markdown(input_paths.basename, edited_plain_text)
        document_result = BackendDocumentRefinementResult(
            backend=backend,
            model_name=model_name or backend,
            final_markdown=final_markdown,
            refinement_strategy="programmatic_markdown_fallback",
            refinement_reason="markdown_content_changed",
            needs_review_sections=document_result.needs_review_sections,
            refinement_notes=document_result.refinement_notes,
            section_map=[],
        )

    all_notes = [item for item in refinement_notes + document_result.refinement_notes if item]
    return BackendDocumentRefinementResult(
        backend=backend,
        model_name=model_name or document_result.model_name or backend,
        final_markdown=document_result.final_markdown if document_result.final_markdown == final_markdown else final_markdown,
        refinement_strategy=document_result.refinement_strategy or "two_step_conservative_refine",
        refinement_reason=document_result.refinement_reason or f"{backend}_two_step_refine",
        needs_review_sections=needs_review_sections + document_result.needs_review_sections,
        refinement_notes=all_notes,
        edited_plain_text=edited_plain_text,
        edit_operations=edit_operations,
        deletion_candidates=deletion_candidates,
        fidelity_report=fidelity_report,
        section_map=document_result.section_map,
    )


def run_single_pass_backend_refinement(
    *,
    backend: str,
    fallback_backend: str | None = None,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    markdown_prompt_text: str,
    asr_full_text: str,
    reference_full_text: str,
    logger: logging.Logger | None = None,
    diagnostic_run_id: str | None = None,
    diagnostic_attempt: int = 1,
) -> BackendDocumentRefinementResult:
    run_id = diagnostic_run_id or create_trace_run_id()
    request_trace = build_refine_request_trace(
        loaded_settings.path_for("logs_dir"),
        basename=input_paths.basename,
        backend=backend,
        run_id=run_id,
        attempt=diagnostic_attempt,
    )
    request_trace.write_json(
        "attempt-meta.json",
        {
            "schema_version": TRACE_SCHEMA_VERSION,
            "stage": "refine",
            "recorded_at": trace_now_iso(),
            "run_id": run_id,
            "attempt": diagnostic_attempt,
            "file": input_paths.basename,
            "requested_backend": backend,
            "fallback_backend": fallback_backend,
            "source_asr_file": relativize_path(input_paths.asr_text_path, loaded_settings.project_root),
            "source_reference_file": relativize_optional_reference(input_paths, loaded_settings),
            **asr_input_metadata(input_paths, loaded_settings),
        },
    )
    # 双 ASR 提供原始候选，不先用可能含 OCR 错误的附件改写/锁定其中一份。
    pre_replaced_segments = [] if input_paths.dual_asr_input is not None else build_pre_replaced_document(
        asr_full_text=asr_full_text,
        reference_full_text=reference_full_text,
        loaded_settings=loaded_settings,
    )
    edited_plain_text = asr_full_text if input_paths.dual_asr_input is not None else build_pre_replaced_plain_text(pre_replaced_segments)

    def run_backend_attempt(target_backend: str) -> BackendDocumentRefinementResult:
        prompt = build_single_pass_refine_prompt(
            markdown_prompt_text,
            input_paths,
            backend=target_backend,
            pre_replaced_segments=pre_replaced_segments,
            reference_full_text=reference_full_text,
        )
        payload = run_backend_payload(
            target_backend,
            prompt,
            loaded_settings,
            request_trace=request_trace if target_backend == BACKEND_CODEX_API else None,
        )
        try:
            return parse_backend_document_result(target_backend, payload)
        except CLIBackendError as exc:
            request_trace.write_json(
                "result-contract-error.json",
                exception_metadata(exc, category="backend_result_contract"),
            )
            raise

    def build_programmatic_fallback_result(target_backend: str) -> BackendDocumentRefinementResult:
        return BackendDocumentRefinementResult(
            backend=target_backend,
            model_name=target_backend,
            final_markdown=build_simple_markdown(input_paths.basename, edited_plain_text),
            refinement_strategy="programmatic_markdown_fallback",
            refinement_reason="single_pass_backend_failed",
            needs_review_sections=[],
            refinement_notes=["single_pass_backend_failed_use_programmatic_fallback"],
            edited_plain_text=edited_plain_text,
            section_map=[],
        )

    fallback_from_backend: str | None = None
    try:
        document_result = run_backend_attempt(backend)
    except CLIBackendError as primary_exc:
        request_trace.write_json(
            "primary-backend-error.json",
            exception_metadata(primary_exc, category="primary_backend"),
        )
        if fallback_backend:
            try:
                document_result = run_backend_attempt(fallback_backend)
                fallback_from_backend = backend
            except CLIBackendError as fallback_exc:
                request_trace.write_json(
                    "fallback-backend-error.json",
                    exception_metadata(fallback_exc, category="fallback_backend"),
                )
                fallback_result = build_programmatic_fallback_result(backend)
                request_trace.write_json(
                    "attempt-outcome.json",
                    {
                        "schema_version": TRACE_SCHEMA_VERSION,
                        "recorded_at": trace_now_iso(),
                        "status": "programmatic_fallback",
                        "refinement_strategy": fallback_result.refinement_strategy,
                        "refinement_reason": fallback_result.refinement_reason,
                    },
                )
                return fallback_result
        else:
            fallback_result = build_programmatic_fallback_result(backend)
            request_trace.write_json(
                "attempt-outcome.json",
                {
                    "schema_version": TRACE_SCHEMA_VERSION,
                    "recorded_at": trace_now_iso(),
                    "status": "programmatic_fallback",
                    "refinement_strategy": fallback_result.refinement_strategy,
                    "refinement_reason": fallback_result.refinement_reason,
                },
            )
            return fallback_result

    refinement_notes = list(document_result.refinement_notes)
    final_markdown = document_result.final_markdown
    if not locked_quotes_preserved(final_markdown, pre_replaced_segments):
        refinement_notes.append("locked_quote_changed_use_programmatic_fallback")
        request_trace.write_json(
            "locked-quote-validation.json",
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "recorded_at": trace_now_iso(),
                "passed": False,
                "reason": "locked_quote_changed",
            },
        )
        final_markdown = build_simple_markdown(input_paths.basename, edited_plain_text)
        document_result = BackendDocumentRefinementResult(
            backend=document_result.backend,
            model_name=document_result.model_name or document_result.backend,
            final_markdown=final_markdown,
            refinement_strategy="programmatic_markdown_fallback",
            refinement_reason="locked_quote_changed",
            needs_review_sections=document_result.needs_review_sections,
            refinement_notes=document_result.refinement_notes,
            section_map=[],
        )

    backend_statuses = dict(document_result.backend_statuses)
    if fallback_from_backend:
        backend_statuses[fallback_from_backend] = "failed_on_file"
        backend_statuses[document_result.backend] = build_result_backend_status(
            document_result,
            fallback_from=fallback_from_backend,
        )

    if logger is not None:
        locked_count = len([segment for segment in pre_replaced_segments if segment.segment_type == "locked_quote"])
        logger.info(
            "阶段 6 单次调用 | file=%s | backend=%s | actual_backend=%s | locked_segments=%s | total_segments=%s",
            input_paths.basename,
            backend,
            document_result.backend,
            locked_count,
            len(pre_replaced_segments),
        )

    final_result = BackendDocumentRefinementResult(
        backend=document_result.backend,
        model_name=document_result.model_name or document_result.backend,
        final_markdown=final_markdown,
        refinement_strategy=(
            document_result.refinement_strategy
            if document_result.refinement_strategy == "programmatic_markdown_fallback"
            else "single_pass_dual_asr" if input_paths.dual_asr_input is not None else "single_pass_safe_replace"
        ),
        refinement_reason=(
            document_result.refinement_reason
            if document_result.refinement_strategy == "programmatic_markdown_fallback"
            else f"single_pass_{document_result.backend}"
        ),
        needs_review_sections=document_result.needs_review_sections,
        refinement_notes=refinement_notes,
        edited_plain_text=edited_plain_text,
        fidelity_report={"passed": True, "pre_replaced_segments": len(pre_replaced_segments)},
        section_map=document_result.section_map,
        backend_statuses=backend_statuses,
    )
    request_trace.write_json(
        "attempt-outcome.json",
        {
            "schema_version": TRACE_SCHEMA_VERSION,
            "recorded_at": trace_now_iso(),
            "status": (
                "programmatic_fallback"
                if final_result.refinement_strategy == "programmatic_markdown_fallback"
                else "backend_result"
            ),
            "actual_backend": final_result.backend,
            "model_name": final_result.model_name,
            "refinement_strategy": final_result.refinement_strategy,
            "refinement_reason": final_result.refinement_reason,
        },
    )
    return final_result


def validate_final_markdown_contract(
    result: BackendDocumentRefinementResult,
) -> list[str]:
    """只拒绝可确定的占位/损坏结果，不把标题或段落样式当成交付契约。"""
    final_markdown = result.final_markdown.strip()
    reasons: list[str] = []
    if result.refinement_strategy == "programmatic_markdown_fallback":
        reasons.append("programmatic_markdown_fallback")
        if result.refinement_reason == "locked_quote_changed":
            reasons.append("locked_quote_changed")
    if "\ufffd" in final_markdown:
        reasons.append("contains_unicode_replacement_character")

    lines = final_markdown.splitlines()
    first_line = next((line.strip() for line in lines if line.strip()), "")
    heading_match = re.fullmatch(r"#\s+(.+)", first_line)
    if heading_match is not None:
        title = normalize_inline_text(heading_match.group(1))
        if title.casefold() == "source":
            reasons.append("uses_canonical_source_placeholder_title")
    return reasons


def build_validation_retry_prompt(markdown_prompt_text: str, reasons: list[str]) -> str:
    rendered_reasons = "、".join(reasons)
    locked_quote_guidance = (
        [
            "锁定原文校验失败：必须保留每个 locked_quote 的全部实词、标题文字和顺序；"
            "只允许调整标点、断句和 Markdown 格式，不得删除、替换或插入实词。"
        ]
        if "locked_quote_changed" in reasons else []
    )
    return "\n\n".join(
        [
            markdown_prompt_text.strip(),
            "## 上一次校对结果未通过交付校验，必须重新完整润色",
            f"检测原因：{rendered_reasons}。",
            *locked_quote_guidance,
            "这不是让你解释错误；请重新输出完整 JSON，且 final_markdown 必须是可直接交付的 Markdown。",
            "final_markdown 不得使用 `# source` 等内部占位标题；如果输出一级标题，必须使用真实内容标题。",
            "不得保留 Unicode 替换字符 `�`，应结合 ASR 和参考原文纠正明显损坏的文字。",
            "除 JSON 外不要输出任何说明、道歉或分析。",
        ]
    )


def run_validated_single_pass_backend_refinement(
    *,
    backend: str,
    fallback_backend: str | None = None,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    markdown_prompt_text: str,
    asr_full_text: str,
    reference_full_text: str,
    logger: logging.Logger | None = None,
) -> BackendDocumentRefinementResult:
    """按配置重试不符合交付契约的精修结果，并保留可追踪的校验信息。"""
    retry_limit = loaded_settings.settings.llm.refinement_validation_retry_count
    retry_count = 0
    validation_failures: list[str] = []
    current_prompt = markdown_prompt_text
    run_id = create_trace_run_id()
    logs_dir = loaded_settings.path_for("logs_dir")

    def persist_validation_attempt(
        *,
        attempt: int,
        status: str,
        reasons: list[str],
        result: BackendDocumentRefinementResult | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        request_trace = build_refine_request_trace(
            logs_dir,
            basename=input_paths.basename,
            backend=backend,
            run_id=run_id,
            attempt=attempt,
        )
        validation_payload: dict[str, Any] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "recorded_at": trace_now_iso(),
            "run_id": run_id,
            "attempt": attempt,
            "status": status,
            "validation_reasons": list(reasons),
        }
        if result is not None:
            validation_payload.update(
                {
                    "actual_backend": result.backend,
                    "model_name": result.model_name,
                    "refinement_strategy": result.refinement_strategy,
                    "refinement_reason": result.refinement_reason,
                }
            )
        if error is not None:
            validation_payload["error"] = exception_metadata(error, category="refine_attempt")
        diagnostic_details = describe_refine_attempt(
            request_trace,
            reasons=reasons,
            markdown=result.final_markdown if result is not None else "",
            error=error,
            refinement_reason=result.refinement_reason if result is not None else None,
        )
        validation_payload.update(diagnostic_details)
        request_trace.write_json("validation.json", validation_payload)

        sse_summary = request_trace.read_json("sse-summary.json")
        transport_payload = request_trace.read_json("transport.json")
        raw_transports = transport_payload.get("transports", [])
        transports = (
            [item for item in raw_transports if isinstance(item, dict)]
            if isinstance(raw_transports, list)
            else []
        )
        summary_entry: dict[str, Any] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "recorded_at": trace_now_iso(),
            "run_id": run_id,
            "file": input_paths.basename,
            "backend": backend,
            "attempt": attempt,
            "status": status,
            "validation_reasons": list(reasons),
            "response_ids": sse_summary.get("response_ids", []),
            "terminal_event": sse_summary.get("terminal_event"),
            "transport_results": [
                {
                    "sequence": item.get("sequence"),
                    "transport": item.get("transport"),
                    "result": item.get("result"),
                    "http_status": item.get("http_status"),
                    "return_code": item.get("return_code"),
                    "metrics": item.get("metrics", {}),
                }
                for item in transports
            ],
            "diagnostic_directory": str(request_trace.directory.relative_to(logs_dir)),
        }
        if result is not None:
            summary_entry.update(
                {
                    "actual_backend": result.backend,
                    "model_name": result.model_name,
                    "refinement_strategy": result.refinement_strategy,
                    "refinement_reason": result.refinement_reason,
                }
            )
        if error is not None:
            summary_entry["error"] = exception_metadata(error, category="refine_attempt")
        summary_entry.update(diagnostic_details)
        update_refine_diagnostics_summary(logs_dir, summary_entry)
        return summary_entry

    while True:
        attempt = retry_count + 1
        try:
            result = run_single_pass_backend_refinement(
                backend=backend,
                fallback_backend=fallback_backend,
                input_paths=input_paths,
                loaded_settings=loaded_settings,
                markdown_prompt_text=current_prompt,
                asr_full_text=asr_full_text,
                reference_full_text=reference_full_text,
                logger=logger,
                diagnostic_run_id=run_id,
                diagnostic_attempt=attempt,
            )
        except CLIBackendError as exc:
            persist_validation_attempt(
                attempt=attempt,
                status="backend_error",
                reasons=[],
                error=exc,
            )
            raise
        reasons = validate_final_markdown_contract(result)
        attempt_diagnostics = persist_validation_attempt(
            attempt=attempt,
            status="accepted" if not reasons else "rejected",
            reasons=reasons,
            result=result,
        )
        if not reasons:
            if retry_count == 0:
                return result
            return replace(
                result,
                refinement_notes=[*result.refinement_notes, "validation_retry_succeeded"],
                validation_retry_count=retry_count,
                validation_failure_reasons=validation_failures,
            )

        validation_failures.extend(reasons)
        if retry_count >= retry_limit:
            detail = "、".join(reasons)
            raise RefinementOutputValidationError(
                f"阶段 6 输出未通过交付校验: file={input_paths.basename}; reasons={detail}; "
                f"原因={attempt_diagnostics['failure_summary']}; "
                f"诊断目录={attempt_diagnostics['diagnostic_directory']}"
            )

        retry_count += 1
        current_prompt = build_validation_retry_prompt(markdown_prompt_text, reasons)
        if logger is not None:
            logger.warning(
                "阶段 6 输出校验失败，准备自动重试 | file=%s | backend=%s | attempt=%s | reasons=%s | 原因=%s",
                input_paths.basename,
                backend,
                retry_count,
                ",".join(reasons),
                attempt_diagnostics["failure_summary"],
            )


def build_fallback_document_result(title: str, asr_full_text: str) -> BackendDocumentRefinementResult:
    review_sections = []
    paragraphs = [item.strip() for item in asr_full_text.splitlines() if item.strip()]
    for paragraph in paragraphs[:5]:
        excerpt = truncate_for_prompt(paragraph, 80)
        if excerpt:
            review_sections.append({"excerpt": excerpt, "reason": "fallback_review_from_asr"})

    final_markdown = f"# {title}\n\n{normalize_multiline_text(asr_full_text)}".strip()

    return BackendDocumentRefinementResult(
        backend=BACKEND_FALLBACK,
        model_name=BACKEND_FALLBACK,
        final_markdown=final_markdown,
        refinement_strategy="keep_asr_full_text",
        refinement_reason="preserve_original_wording_for_safe_fallback",
        needs_review_sections=review_sections,
        refinement_notes=["all_cli_backends_failed_use_fallback"],
        edited_plain_text=normalize_multiline_text(asr_full_text),
        fidelity_report={"passed": True, "reasons": ["fallback_from_cli_failure"]},
    )


def calculate_document_score(
    *,
    asr_full_text: str,
    reference_full_text: str,
    result: BackendDocumentRefinementResult,
) -> float:
    refined_text = normalize_inline_text(markdown_to_plain_text(result.final_markdown))
    asr_similarity = fuzz.ratio(refined_text, normalize_inline_text(asr_full_text)) if asr_full_text else 0.0
    reference_plain_text = normalize_inline_text(strip_ocr_page_break_markers(reference_full_text))
    reference_similarity = fuzz.ratio(refined_text, reference_plain_text) if reference_plain_text else 0.0
    reference_weight = 0.6 if reference_plain_text else 0.0
    asr_weight = 1.0 - reference_weight
    penalty = min(len(result.needs_review_sections), 5) * 2.0
    return round(asr_similarity * asr_weight + reference_similarity * reference_weight - penalty, 2)


def compare_backend_documents(
    *,
    asr_full_text: str,
    reference_full_text: str,
    candidates: list[BackendDocumentRefinementResult],
) -> tuple[BackendDocumentRefinementResult, str]:
    if len(candidates) == 1 and candidates[0].backend == BACKEND_FALLBACK:
        return candidates[0], "all_cli_backends_failed_use_fallback"

    scored = [
        (calculate_document_score(asr_full_text=asr_full_text, reference_full_text=reference_full_text, result=result), result)
        for result in candidates
        if result.final_markdown
    ]
    if not scored:
        fallback = build_fallback_document_result("未命名讲解", asr_full_text)
        return fallback, "no_cli_result_available_use_fallback"

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_result = scored[0]
    if len(scored) == 1:
        return best_result, f"single_backend_selected:{best_result.backend}:{best_score}"

    second_score, second_result = scored[1]
    return best_result, f"selected={best_result.backend}:{best_score};runner_up={second_result.backend}:{second_score}"


def serialize_backend_result(result: BackendDocumentRefinementResult) -> dict[str, Any]:
    return {
        "backend": result.backend,
        "model_name": result.model_name,
        "final_markdown": result.final_markdown,
        "edited_plain_text": result.edited_plain_text,
        "refined_full_text": markdown_to_plain_text(result.final_markdown),
        "refinement_strategy": result.refinement_strategy,
        "refinement_reason": result.refinement_reason,
        "needs_review_sections": result.needs_review_sections,
        "refinement_notes": result.refinement_notes,
        "edit_operations": result.edit_operations,
        "deletion_candidates": result.deletion_candidates,
        "fidelity_report": result.fidelity_report,
        "section_map": result.section_map,
        "backend_statuses": result.backend_statuses,
        "validation_retry_count": result.validation_retry_count,
        "validation_failure_reasons": result.validation_failure_reasons,
    }


def relativize_optional_reference(input_paths: RefinementInputPaths, loaded_settings: LoadedSettings) -> str | None:
    if input_paths.reference_text_path is None:
        return None
    return relativize_path(input_paths.reference_text_path, loaded_settings.project_root)


def write_backend_result_file(
    *,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    backend_status: str,
    result: BackendDocumentRefinementResult,
    output_path: Path,
) -> None:
    payload = {
        "source_asr_file": relativize_path(input_paths.asr_text_path, loaded_settings.project_root),
        "source_reference_file": relativize_optional_reference(input_paths, loaded_settings),
        **asr_input_metadata(input_paths, loaded_settings),
        "refinement_backends": [result.backend],
        "backend_status": {result.backend: backend_status},
        "prompt_mode": result.refinement_strategy,
        "selected_backend": result.backend,
        "comparison_summary": "single_backend_result",
        **serialize_backend_result(result),
    }
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def write_refinement_result(
    *,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    active_backends: list[str],
    backend_status: dict[str, str],
    backend_results: list[BackendDocumentRefinementResult],
    selected_result: BackendDocumentRefinementResult | None,
    comparison_summary: str,
    output_path: RefinementOutputPath,
) -> None:
    ensure_directory(output_path.json_path.parent)
    model_results = {result.backend: serialize_backend_result(result) for result in backend_results}
    base_result = selected_result or backend_results[0]
    final_markdown = selected_result.final_markdown if selected_result else ""
    payload = {
        "source_asr_file": relativize_path(input_paths.asr_text_path, loaded_settings.project_root),
        "source_reference_file": relativize_optional_reference(input_paths, loaded_settings),
        **asr_input_metadata(input_paths, loaded_settings),
        "refinement_backends": list(active_backends),
        "backend_status": backend_status,
        "prompt_mode": selected_result.refinement_strategy if selected_result else "manual_selection_required",
        "selected_backend": selected_result.backend if selected_result else None,
        "comparison_summary": comparison_summary,
        "final_markdown": final_markdown,
        "edited_plain_text": base_result.edited_plain_text,
        "refined_full_text": markdown_to_plain_text(final_markdown),
        "refinement_strategy": selected_result.refinement_strategy if selected_result else "manual_selection_required",
        "refinement_reason": selected_result.refinement_reason if selected_result else "manual_selection_required",
        "needs_review_sections": selected_result.needs_review_sections if selected_result else [],
        "refinement_notes": selected_result.refinement_notes if selected_result else ["manual_selection_required"],
        "edit_operations": selected_result.edit_operations if selected_result else [],
        "deletion_candidates": selected_result.deletion_candidates if selected_result else [],
        "fidelity_report": selected_result.fidelity_report if selected_result else {},
        "section_map": selected_result.section_map if selected_result else [],
        "model_results": model_results,
    }
    with output_path.json_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

    for result in backend_results:
        if result.backend == BACKEND_FALLBACK:
            continue
        write_backend_result_file(
            input_paths=input_paths,
            loaded_settings=loaded_settings,
            backend_status=backend_status.get(result.backend, build_result_backend_status(result)),
            result=result,
            output_path=build_backend_output_json_path(output_path, result.backend),
        )


def refine_batch(
    loaded_settings: LoadedSettings,
    requested_backends: list[str] | None = None,
    logger: logging.Logger | None = None,
) -> RefinementBatchSummary:
    asr_dir = loaded_settings.path_for("asr_dir")
    output_dir = ensure_directory(loaded_settings.path_for("refined_dir"))
    asr_files = iter_asr_text_files(asr_dir)
    active_backends = list(requested_backends) if requested_backends is not None else list(loaded_settings.settings.llm.backends)

    if not asr_files:
        raise RefinementInputEmptyError(f"ASR 输入目录中没有可处理的 TXT 文件: {asr_dir}")

    markdown_prompt_text = load_markdown_assemble_prompt(loaded_settings)
    items: list[RefinementBatchItem] = []
    success_count = 0

    for asr_text_path in asr_files:
        input_paths = resolve_refinement_input_paths(loaded_settings, asr_text_path)
        asr_full_text = load_text_file(input_paths.asr_text_path, "ASR")
        reference_full_text = (
            load_text_file(input_paths.reference_text_path, "参考原文")
            if input_paths.reference_text_path is not None
            else ""
        )
        backend_results: list[BackendDocumentRefinementResult] = []
        backend_status: dict[str, str] = {}
        validation_errors: list[str] = []
        document_title = input_paths.basename
        for backend in active_backends:
            try:
                result = run_validated_single_pass_backend_refinement(
                    backend=backend,
                    fallback_backend=(
                        BACKEND_CODEX
                        if backend == BACKEND_AGY
                        and BACKEND_CODEX not in active_backends
                        and BACKEND_CODEX_API not in active_backends
                        else None
                    ),
                    input_paths=input_paths,
                    loaded_settings=loaded_settings,
                    markdown_prompt_text=markdown_prompt_text,
                    asr_full_text=asr_full_text,
                    reference_full_text=reference_full_text,
                    logger=logger,
                )
            except (CLIBackendError, RefinementOutputValidationError) as exc:
                backend_status[backend] = "failed_on_file"
                if isinstance(exc, RefinementOutputValidationError):
                    validation_errors.append(str(exc))
                if logger:
                    logger.warning("阶段 6 后端失败 | backend=%s | file=%s | %s", backend, input_paths.basename, exc)
                continue

            backend_results.append(result)
            backend_status.update(resolve_backend_statuses(result))

        if not backend_results and validation_errors:
            raise RefinementOutputValidationError("; ".join(validation_errors))

        if not backend_results and loaded_settings.settings.llm.enable_fallback:
            backend_results = [build_fallback_document_result(document_title, asr_full_text)]
            backend_status["fallback"] = "used"

        if not backend_results:
            raise CLIBackendError(f"阶段 6 所有后端均未返回结果，且未启用 fallback: {input_paths.basename}.txt")

        selected_result: BackendDocumentRefinementResult | None
        comparison_summary: str
        if len([result for result in backend_results if result.backend != BACKEND_FALLBACK]) > 1:
            selected_result = None
            comparison_summary = "manual_selection_required"
        else:
            selected_result, comparison_summary = compare_backend_documents(
                asr_full_text=asr_full_text,
                reference_full_text=reference_full_text,
                candidates=backend_results,
            )
        output_path = build_refinement_output_path(asr_text_path, output_dir)
        write_refinement_result(
            input_paths=input_paths,
            loaded_settings=loaded_settings,
            active_backends=active_backends,
            backend_status=backend_status,
            backend_results=backend_results,
            selected_result=selected_result,
            comparison_summary=comparison_summary,
            output_path=output_path,
        )

        items.append(
            RefinementBatchItem(
                basename=input_paths.basename,
                output_path=output_path.json_path,
                success=True,
                skipped=False,
                selected_backends=[result.backend for result in backend_results],
            )
        )
        success_count += 1
        if logger:
            logger.info(
                "精修完成 | %s | prompt_mode=%s | needs_review=%s | selected_backend=%s",
                input_paths.basename,
                selected_result.refinement_strategy if selected_result else "manual_selection_required",
                len(selected_result.needs_review_sections) if selected_result else 0,
                selected_result.backend if selected_result else "manual_selection_required",
            )

    return RefinementBatchSummary(
        total=len(asr_files),
        success=success_count,
        skipped=0,
        failed=0,
        backends=active_backends,
        items=items,
    )


def summarize_refinement_results(summary: RefinementBatchSummary) -> str:
    return (
        f"total={summary.total}, success={summary.success}, skipped={summary.skipped}, "
        f"failed={summary.failed}, backends={','.join(summary.backends)}"
    )

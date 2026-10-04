from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.ocr_page_markers import strip_ocr_page_break_markers
from src.runtime_utils import ensure_directory, relativize_path
from src.schemas import LoadedSettings


class AlignmentError(RuntimeError):
    """Raised when alignment fails."""


class AlignmentInputEmptyError(AlignmentError):
    """Raised when there are no ASR inputs to align."""


@dataclass(frozen=True)
class AsrReferencePair:
    basename: str
    asr_json_path: Path
    reference_txt_path: Path | None


@dataclass(frozen=True)
class AsrBlock:
    block_id: int
    source_segment_ids: list[int]
    start: float
    end: float
    asr_text: str


@dataclass(frozen=True)
class ReferenceBlock:
    ref_block_id: int
    reference_text: str


@dataclass(frozen=True)
class TopMatch:
    ref_block_id: int
    reference_text: str
    score: float


@dataclass(frozen=True)
class AlignedBlock:
    block_id: int
    source_segment_ids: list[int]
    start: float
    end: float
    asr_text: str
    matched_reference_text: str
    match_score: float
    match_status: str
    top_matches: list[TopMatch]


@dataclass(frozen=True)
class AlignmentOutputPath:
    json_path: Path


@dataclass(frozen=True)
class AlignmentBatchItem:
    basename: str
    output_path: Path | None
    success: bool
    skipped: bool
    best_score: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AlignmentBatchSummary:
    total: int
    success: int
    skipped: int
    failed: int
    items: list[AlignmentBatchItem]


def iter_asr_json_files(asr_dir: Path) -> list[Path]:
    if not asr_dir.exists():
        return []
    return sorted(path for path in asr_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json")


def iter_reference_txt_files(reference_dir: Path) -> list[Path]:
    if not reference_dir.exists():
        return []
    return sorted(path for path in reference_dir.iterdir() if path.is_file() and path.suffix.lower() == ".txt")


def match_paired_files(asr_files: list[Path], reference_files: list[Path]) -> list[AsrReferencePair]:
    reference_map = {path.stem: path for path in reference_files}
    return [
        AsrReferencePair(
            basename=asr_path.stem,
            asr_json_path=asr_path,
            reference_txt_path=reference_map.get(asr_path.stem),
        )
        for asr_path in asr_files
    ]


def build_alignment_output_path(asr_json_path: Path, output_dir: Path) -> AlignmentOutputPath:
    return AlignmentOutputPath(json_path=output_dir / f"{asr_json_path.stem}.json")


def load_asr_payload(asr_json_path: Path) -> dict[str, Any]:
    try:
        with asr_json_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except OSError as exc:
        raise AlignmentError(f"无法读取 ASR 文件: {asr_json_path.name} | {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AlignmentError(f"ASR JSON 解析失败: {asr_json_path.name} | {exc}") from exc

    if not isinstance(payload, dict) or "segments" not in payload:
        raise AlignmentError(f"ASR JSON 结构无效: {asr_json_path.name}")
    return payload


def load_reference_text(reference_txt_path: Path) -> str:
    try:
        return reference_txt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AlignmentError(f"无法读取参考文本文件: {reference_txt_path.name} | {exc}") from exc


def build_asr_blocks(asr_segments: list[dict[str, Any]], loaded_settings: LoadedSettings) -> list[AsrBlock]:
    segmentation = loaded_settings.settings.segmentation
    blocks: list[AsrBlock] = []

    current_segment_ids: list[int] = []
    current_texts: list[str] = []
    current_start: float | None = None
    current_end: float | None = None

    def flush_block() -> None:
        nonlocal current_segment_ids, current_texts, current_start, current_end
        if not current_segment_ids:
            return
        blocks.append(
            AsrBlock(
                block_id=len(blocks) + 1,
                source_segment_ids=current_segment_ids,
                start=float(current_start if current_start is not None else 0.0),
                end=float(current_end if current_end is not None else 0.0),
                asr_text=" ".join(text for text in current_texts if text).strip(),
            )
        )
        current_segment_ids = []
        current_texts = []
        current_start = None
        current_end = None

    for segment in asr_segments:
        segment_text = str(segment.get("text", "")).strip()
        segment_id = int(segment.get("id", len(blocks)))
        segment_start = float(segment.get("start", 0.0))
        segment_end = float(segment.get("end", segment_start))

        candidate_texts = current_texts + ([segment_text] if segment_text else [])
        candidate_text = " ".join(text for text in candidate_texts if text).strip()
        candidate_start = current_start if current_start is not None else segment_start
        candidate_end = segment_end
        candidate_seconds = candidate_end - candidate_start

        should_split = current_segment_ids and (
            len(candidate_text) > segmentation.max_chars_per_block
            or candidate_seconds > segmentation.max_seconds_per_block
        )
        current_text = " ".join(text for text in current_texts if text).strip()
        if should_split and (
            len(current_text) >= segmentation.min_chars_per_block or len(current_segment_ids) > 1
        ):
            flush_block()

        if current_start is None:
            current_start = segment_start
        current_end = segment_end
        current_segment_ids.append(segment_id)
        if segment_text:
            current_texts.append(segment_text)

    flush_block()
    return blocks


def split_text_by_punctuation(text: str, max_chars_per_block: int) -> list[str]:
    sentences = re.split(r"(?<=[。！？!?；;])", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        part = sentence.strip()
        if not part:
            continue
        candidate = f"{current}{part}" if current else part
        if current and len(candidate) > max_chars_per_block:
            chunks.append(current.strip())
            current = part
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def split_reference_paragraph_into_sentence_blocks(text: str, max_chars_per_block: int) -> list[str]:
    sentences = re.split(r"(?<=[。！？!?；;])", text)
    parts = [sentence.strip() for sentence in sentences if sentence.strip()]
    blocks: list[str] = []

    for part in parts:
        if len(part) <= max_chars_per_block:
            blocks.append(part)
            continue

        blocks.extend(split_text_by_punctuation(part, max_chars_per_block))

    return [block for block in blocks if block.strip()]


def split_reference_text(reference_text: str, loaded_settings: LoadedSettings) -> list[str]:
    # 工程页标记仅供全文校对理解，不能成为正文对齐候选；不回写源 TXT。
    reference_text = strip_ocr_page_break_markers(reference_text)
    segmentation = loaded_settings.settings.segmentation
    reference_settings = loaded_settings.settings.reference

    if segmentation.split_on_empty_line:
        raw_parts = [part.strip() for part in re.split(r"\n\s*\n", reference_text) if part.strip()]
    else:
        raw_parts = [reference_text.strip()] if reference_text.strip() else []

    split_parts: list[str] = []
    for part in raw_parts:
        lines = [line.strip() for line in part.splitlines() if line.strip()]
        if len(lines) > 1 and len(lines[0]) < segmentation.min_chars_per_block:
            split_parts.append(lines[0])
            remaining_text = "\n".join(lines[1:]).strip()
            if not remaining_text:
                continue
            if reference_settings.sentence_split_enabled:
                split_parts.extend(
                    split_reference_paragraph_into_sentence_blocks(
                        remaining_text,
                        segmentation.max_chars_per_block,
                    )
                )
            else:
                split_parts.append(remaining_text)
        elif reference_settings.sentence_split_enabled:
            split_parts.extend(
                split_reference_paragraph_into_sentence_blocks(
                    part,
                    segmentation.max_chars_per_block,
                )
            )
        else:
            split_parts.append(part)

    return [part.strip() for part in split_parts if part.strip()]


def normalize_text_width(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def normalize_text_symbols(text: str) -> str:
    normalized = text.replace("•", ".").replace("·", ".").replace("．", ".")
    normalized = re.sub(r"[“”\"'`]", "", normalized)
    normalized = re.sub(r"[，,、；;：:]", "", normalized)
    normalized = re.sub(r"[。！？!?（）()\[\]{}<>《》〈〉\-—_]", "", normalized)
    return normalized


def normalize_text_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text).strip()


def normalize_text_for_matching(text: str) -> str:
    normalized = normalize_text_width(text)
    normalized = normalize_text_symbols(normalized)
    normalized = normalize_text_whitespace(normalized)
    return normalized.lower()


def build_reference_blocks(reference_text: str, loaded_settings: LoadedSettings) -> list[ReferenceBlock]:
    split_parts = split_reference_text(reference_text, loaded_settings)
    return [
        ReferenceBlock(ref_block_id=index + 1, reference_text=part)
        for index, part in enumerate(split_parts)
        if part.strip()
    ]


def score_block_match(
    asr_text: str,
    reference_text: str,
    *,
    method: str,
    use_normalization: bool,
) -> float:
    if method != "rapidfuzz_ratio":
        raise AlignmentError(f"当前阶段仅支持 rapidfuzz_ratio，配置值为: {method}")

    try:
        from rapidfuzz import fuzz
    except ImportError as exc:
        raise AlignmentError("未安装 rapidfuzz。请先执行 `pip install -r requirements.txt`。") from exc

    left = normalize_text_for_matching(asr_text) if use_normalization else asr_text
    right = normalize_text_for_matching(reference_text) if use_normalization else reference_text
    ratio_score = float(fuzz.ratio(left, right))
    partial_score = float(fuzz.partial_ratio(left, right))
    weighted_ratio = float(fuzz.WRatio(left, right))
    return round((ratio_score * 0.35) + (partial_score * 0.4) + (weighted_ratio * 0.25), 2)


def determine_match_status(score: float, loaded_settings: LoadedSettings) -> str:
    alignment = loaded_settings.settings.alignment
    if score >= alignment.matched_threshold:
        return "matched"
    if score >= alignment.weak_match_threshold:
        return "weak_match"
    return "no_match"


def align_blocks(
    asr_blocks: list[AsrBlock],
    reference_blocks: list[ReferenceBlock],
    loaded_settings: LoadedSettings,
) -> list[AlignedBlock]:
    aligned_blocks: list[AlignedBlock] = []
    alignment_settings = loaded_settings.settings.alignment

    for asr_block in asr_blocks:
        matches: list[TopMatch] = []

        for reference_block in reference_blocks:
            score = score_block_match(
                asr_text=asr_block.asr_text,
                reference_text=reference_block.reference_text,
                method=alignment_settings.method,
                use_normalization=alignment_settings.use_normalization,
            )
            matches.append(
                TopMatch(
                    ref_block_id=reference_block.ref_block_id,
                    reference_text=reference_block.reference_text,
                    score=score,
                )
            )

        matches.sort(key=lambda item: item.score, reverse=True)
        top_matches = matches[: alignment_settings.top_k]
        best_match = top_matches[0] if top_matches else TopMatch(0, "", 0.0)

        aligned_blocks.append(
            AlignedBlock(
                block_id=asr_block.block_id,
                source_segment_ids=asr_block.source_segment_ids,
                start=asr_block.start,
                end=asr_block.end,
                asr_text=asr_block.asr_text,
                matched_reference_text=best_match.reference_text,
                match_score=best_match.score,
                match_status=determine_match_status(best_match.score, loaded_settings),
                top_matches=top_matches,
            )
        )

    return aligned_blocks


def calculate_average_best_score(aligned_blocks: list[AlignedBlock]) -> float:
    if not aligned_blocks:
        return 0.0
    return round(sum(block.match_score for block in aligned_blocks) / len(aligned_blocks), 2)


def write_alignment_result(
    *,
    asr_json_path: Path,
    reference_txt_path: Path,
    aligned_blocks: list[AlignedBlock],
    reference_blocks: list[ReferenceBlock],
    output_path: AlignmentOutputPath,
    loaded_settings: LoadedSettings,
) -> None:
    ensure_directory(output_path.json_path.parent)
    payload = {
        "source_asr_file": relativize_path(asr_json_path, loaded_settings.project_root),
        "source_reference_file": relativize_path(reference_txt_path, loaded_settings.project_root),
        "alignment_method": loaded_settings.settings.alignment.method,
        "total_asr_blocks": len(aligned_blocks),
        "total_reference_blocks": len(reference_blocks),
        "blocks": [asdict(block) for block in aligned_blocks],
    }

    with output_path.json_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def align_batch(
    loaded_settings: LoadedSettings,
    logger: logging.Logger | None = None,
) -> AlignmentBatchSummary:
    asr_dir = loaded_settings.path_for("asr_dir")
    reference_dir = loaded_settings.path_for("extracted_text_dir")
    output_dir = ensure_directory(loaded_settings.path_for("aligned_dir"))

    asr_files = iter_asr_json_files(asr_dir)
    if not asr_files:
        raise AlignmentInputEmptyError(f"ASR 输入目录中没有可处理的 JSON 文件: {asr_dir}")

    reference_files = iter_reference_txt_files(reference_dir)
    if not reference_files:
        raise AlignmentInputEmptyError(f"参考文本目录中没有可处理的 TXT 文件: {reference_dir}")

    pairs = match_paired_files(asr_files, reference_files)
    items: list[AlignmentBatchItem] = []
    success_count = 0
    skipped_count = 0
    failed_count = 0

    for pair in pairs:
        if pair.reference_txt_path is None:
            skipped_count += 1
            items.append(
                AlignmentBatchItem(
                    basename=pair.basename,
                    output_path=None,
                    success=False,
                    skipped=True,
                    reason="missing_reference",
                )
            )
            if logger:
                logger.warning("跳过未配对文件 | %s | 缺少 reference txt", pair.basename)
            continue

        try:
            asr_payload = load_asr_payload(pair.asr_json_path)
            reference_text = load_reference_text(pair.reference_txt_path)
            asr_blocks = build_asr_blocks(asr_payload["segments"], loaded_settings)
            reference_blocks = build_reference_blocks(reference_text, loaded_settings)
            aligned_blocks = align_blocks(asr_blocks, reference_blocks, loaded_settings)
            output_path = build_alignment_output_path(pair.asr_json_path, output_dir)
            write_alignment_result(
                asr_json_path=pair.asr_json_path,
                reference_txt_path=pair.reference_txt_path,
                aligned_blocks=aligned_blocks,
                reference_blocks=reference_blocks,
                output_path=output_path,
                loaded_settings=loaded_settings,
            )
        except AlignmentError:
            failed_count += 1
            items.append(
                AlignmentBatchItem(
                    basename=pair.basename,
                    output_path=None,
                    success=False,
                    skipped=False,
                    reason="alignment_error",
                )
            )
            raise

        success_count += 1
        average_best_score = calculate_average_best_score(aligned_blocks)
        items.append(
            AlignmentBatchItem(
                basename=pair.basename,
                output_path=output_path.json_path,
                success=True,
                skipped=False,
                best_score=average_best_score,
            )
        )
        if logger:
            logger.info(
                "对齐完成 | %s | asr_blocks=%s | reference_blocks=%s | average_best_score=%s",
                pair.basename,
                len(aligned_blocks),
                len(reference_blocks),
                average_best_score,
            )

    return AlignmentBatchSummary(
        total=len(pairs),
        success=success_count,
        skipped=skipped_count,
        failed=failed_count,
        items=items,
    )


def summarize_alignment_results(summary: AlignmentBatchSummary) -> str:
    success_scores = [item.best_score for item in summary.items if item.success and item.best_score is not None]
    average_score = round(sum(success_scores) / len(success_scores), 2) if success_scores else 0.0
    return (
        f"total={summary.total}, success={summary.success}, "
        f"skipped={summary.skipped}, failed={summary.failed}, average_best_score={average_score}"
    )

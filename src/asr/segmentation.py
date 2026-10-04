from __future__ import annotations

import math
from typing import Any


class ZeroDurationAlignmentError(ValueError):
    """All aligned lexical units lack a usable time interval."""


def lexical_text(text: str) -> str:
    return "".join(c.casefold() for c in text if c.isalnum())


def bounded_speech_intervals(regions: list[dict[str, int]], sample_count: int,
                             max_samples: int, *, min_tail_samples: int = 0) -> list[tuple[int, int]]:
    """Split real VAD ranges; optionally rebalance a short artificial tail.

    Only move a boundary within the same real VAD region. Never pad, discard,
    overlap or borrow speech from a different region to satisfy a duration.
    Naturally short regions remain short and are still subject to validation.
    """
    if max_samples <= 0:
        raise ValueError("音频分块长度必须为正数")
    if not 0 <= min_tail_samples <= max_samples:
        raise ValueError("尾块最小时长必须在分块上限内")
    intervals = []
    previous_end = 0
    for region in regions:
        start, end = region["start"], region["end"]
        if not (isinstance(start, int) and isinstance(end, int)
                and previous_end <= start < end <= sample_count):
            raise ValueError("VAD 区间越界、重叠或顺序无效")
        current = [(begin, min(end, begin + max_samples)) for begin in range(start, end, max_samples)]
        if len(current) > 1 and current[-1][1] - current[-1][0] < min_tail_samples:
            previous_start = current[-2][0]
            # Balance the pair, rather than merely making a tiny tail 1s:
            # the recognizer also needs enough real linguistic context.
            boundary = previous_start + (end - previous_start) // 2
            current[-2:] = [(previous_start, boundary), (boundary, end)]
        intervals.extend(current)
        previous_end = end
    return intervals


def aligned_segments(text: str, units: list[tuple[str, float, float]], duration: float) -> list[dict[str, Any]]:
    """Keep actual recognizer punctuation, using only SDK-derived time bounds.

    Fail closed if an aligner normalized/changed lexical content: do not assign
    timestamps to unmatched characters by proportion or guessed word lengths.
    """
    if lexical_text(text) != "".join(lexical_text(u[0]) for u in units):
        raise ValueError("ASR 文本与时间戳词/字不一致，无法可靠恢复标点对应关系")
    positions = [i for i, c in enumerate(text) if c.isalnum()]
    if not positions:
        if text.strip():
            raise ValueError("只有标点而无可对齐的语音文字")
        return []
    pieces = []
    letter_cursor = 0
    text_cursor = 0
    previous_start = -1.0
    for token, start, end in units:
        count = len([c for c in token if c.isalnum()])
        if not count:
            continue
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= duration + 0.1):
            raise ValueError(f"无效 ASR 时间区间: {start}, {end} / {duration}")
        if start < previous_start:
            raise ValueError("ASR 时间戳顺序错误")
        previous_start = start
        letter_cursor += count
        if letter_cursor > len(positions):
            raise ValueError("对齐器规范化后的字数与原文不一致")
        boundary = positions[letter_cursor] if letter_cursor < len(positions) else len(text)
        pieces.append((text[text_cursor:boundary], start, end))
        text_cursor = boundary
    if letter_cursor != len(positions):
        raise ValueError("时间戳未覆盖全部识别文字")
    result = []
    buffer = ""
    begin = 0.0
    finish = 0.0
    for piece, start, end in pieces:
        if not buffer:
            begin = start
            finish = end
        buffer += piece
        finish = max(finish, end)
        if any(c in piece for c in "。！？!?\n") or finish - begin >= 20 or len(buffer) >= 120:
            result.append({"id": len(result) + 1, "start": begin, "end": finish, "text": buffer.strip()})
            buffer = ""
    if buffer.strip():
        result.append({"id": len(result) + 1, "start": begin, "end": finish, "text": buffer.strip()})
    # Some real aligners collapse individual punctuation-delimited phrases.
    # Aggregate them with an adjacent actual interval; never invent durations.
    merged = []
    pending = None
    for segment in result:
        if segment["end"] == segment["start"]:
            if merged:
                merged[-1]["text"] += segment["text"]
                merged[-1]["end"] = max(merged[-1]["end"], segment["end"])
            elif pending is None:
                pending = segment
            else:
                pending["text"] += segment["text"]
                pending["end"] = max(pending["end"], segment["end"])
            continue
        if pending is not None:
            segment["text"] = pending["text"] + segment["text"]
            segment["start"] = min(pending["start"], segment["start"])
            pending = None
        segment["id"] = len(merged) + 1
        merged.append(segment)
    if not merged:
        raise ZeroDurationAlignmentError("非空转录全部只有零时长对齐，无法提供可信音频区间")
    return merged

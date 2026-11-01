"""书库材料接入既有文本阶段的结构适配，不改变普通参考文件。"""
from __future__ import annotations

import json
import re
from pathlib import Path


LIBRARY_REFERENCE_RULES = """书库参考材料的来源边界：
参考文件中的 LIBRARY_FRAGMENT 来源片段及 OCR_PAGE_BREAK 是工程标记，不是最终稿正文。
同一来源片段内的物理换页可按上下文衔接；不同书籍或不连续来源片段不能仅因相邻而合并。
来源书名、版本、页码说明仅用于判断参考归属，不要当作朗读或讲解正文写入最终稿。
"""


def read_library_sources(path: str | Path) -> dict:
    try:
        sources = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取本任务的书库来源清单：{exc}") from exc
    if not isinstance(sources, dict) or sources.get("schema_version") != 1:
        raise ValueError("本任务的书库来源清单格式无效。")
    return sources


def library_reference_fragments(text: str, sources_file: str | Path) -> list[str]:
    """按发布时记录的正文区间拆片；不猜测普通正文里的标题或分隔行。"""
    sources = read_library_sources(sources_file)
    fragments = sources.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        raise ValueError("书库来源清单缺少片段信息。")
    parts = []
    previous_end = 0
    for fragment in fragments:
        start, end = fragment.get("text_start"), fragment.get("text_end")
        if (type(start) is not int or type(end) is not int
                or not previous_end <= start <= end <= len(text)):
            raise ValueError("书库来源清单的正文区间与参考文本不符。")
        parts.append(text[start:end])
        previous_end = end
    return parts


def require_clean_library_output(text: str, sources_file: str | Path) -> None:
    """只拦截确定的工程标记残留；不重写模型正文或自动发起重试。"""
    if re.search(r"(?:LIBRARY_FRAGMENT|OCR_PAGE_BREAK)\s*:", text):
        raise ValueError("校对结果仍包含书库工程标记，未发布最终稿；请检查本次调用结果。")
    sources = read_library_sources(sources_file)
    for fragment in sources.get("fragments", []):
        for line in fragment.get("header", "").splitlines():
            if line.strip() and line.strip() in text:
                raise ValueError("校对结果仍包含书库来源说明，未发布最终稿；请检查本次调用结果。")

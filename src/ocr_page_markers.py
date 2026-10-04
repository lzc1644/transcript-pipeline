"""PDF OCR 合并输出中的工程换页标记；单页检查点不写入此标记。"""
from __future__ import annotations

import re

_PAGE_BREAK_LINE = re.compile(r"----- OCR_PAGE_BREAK: ([1-9][0-9]*) -> ([1-9][0-9]*) -----")


def format_ocr_page_break(previous_page: int) -> str:
    """生成从原 PDF 的指定页到下一页的物理边界标记。"""
    if previous_page < 1:
        raise ValueError("PDF 换页标记的页码必须为正整数。")
    return f"----- OCR_PAGE_BREAK: {previous_page} -> {previous_page + 1} -----"


def is_ocr_page_break_line(line: str) -> bool:
    """只识别完整的独立标记行及相邻原页码，不匹配正文片段。"""
    match = _PAGE_BREAK_LINE.fullmatch(line.strip())
    return bool(match and int(match[2]) == int(match[1]) + 1)


def strip_ocr_page_break_markers(text: str) -> str:
    """在规则匹配的消费边界过滤工程行，保留其他文字和换行。"""
    return "\n".join(line for line in text.split("\n") if not is_ocr_page_break_line(line))

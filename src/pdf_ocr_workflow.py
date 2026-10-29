"""历史配置适配；独立 PDF 身份与恢复规则位于 src.pdf.workflow。"""
from pathlib import Path
from src.pdf.workflow import *  # noqa: F403 — 保留历史类型、常量及函数导入
from src.pdf.workflow import build_pdf_ocr_run_identity as _build_identity
from src.schemas import LoadedSettings


def build_pdf_ocr_run_identity(source_pdf: Path, loaded_settings: LoadedSettings) -> PDFOCRRunIdentity:
    settings = loaded_settings.settings.reference
    return _build_identity(source_pdf, settings.codex_ocr_model, settings.codex_ocr_reasoning_effort)

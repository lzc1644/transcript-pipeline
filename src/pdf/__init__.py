"""不依赖 Web、job 或完整项目配置的 PDF 逐页识别能力。"""
from src.pdf.ocr import (
    PDFOCROptions,
    PDFOCRPagesResult,
    PDFOCRResult,
    build_codex_api_image_ocr_prompt,
    build_pdf_book_ocr_image_prompt,
    merge_pdf_pages,
    ocr_pdf,
    ocr_pdf_pages,
)
from src.pdf.workflow import (
    PDFOCRPageState,
    PDFOCRRetryPlan,
    PDFOCRRunIdentity,
    build_pdf_ocr_checkpoint_namespace,
    build_pdf_ocr_retry_plan,
    build_pdf_ocr_run_identity,
)

__all__ = [
    "PDFOCROptions", "PDFOCRPagesResult", "PDFOCRResult", "PDFOCRPageState", "PDFOCRRetryPlan", "PDFOCRRunIdentity",
    "build_codex_api_image_ocr_prompt", "build_pdf_book_ocr_image_prompt", "merge_pdf_pages", "ocr_pdf", "ocr_pdf_pages",
    "build_pdf_ocr_checkpoint_namespace", "build_pdf_ocr_retry_plan", "build_pdf_ocr_run_identity",
]

"""不依赖 Web、job 或完整项目配置的 PDF 逐页识别能力。"""
from src.pdf.ocr import (
    PDFOCROptions,
    PDFOCRResult,
    build_codex_api_image_ocr_prompt,
    build_pdf_book_ocr_image_prompt,
    merge_pdf_pages,
    ocr_pdf,
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
    "PDFOCROptions", "PDFOCRResult", "PDFOCRPageState", "PDFOCRRetryPlan", "PDFOCRRunIdentity",
    "build_codex_api_image_ocr_prompt", "build_pdf_book_ocr_image_prompt", "merge_pdf_pages", "ocr_pdf",
    "build_pdf_ocr_checkpoint_namespace", "build_pdf_ocr_retry_plan", "build_pdf_ocr_run_identity",
]

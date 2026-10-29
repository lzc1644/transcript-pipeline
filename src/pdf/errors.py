from __future__ import annotations

from pathlib import Path


class ReferencePreparationError(RuntimeError):
    """Raised when reference preparation fails."""


class ReferenceInputEmptyError(ReferencePreparationError):
    """Raised when there are no supported reference files to process."""


class PdfDependencyError(ReferencePreparationError):
    """Raised when PDF extraction dependencies are missing."""


class AgyOCRError(ReferencePreparationError):
    """agy OCR 调用失败。"""


class CodexOCRError(ReferencePreparationError):
    """Codex OCR 调用失败。"""


class CodexOCRPagesIncompleteError(CodexOCRError):
    """单页任务全部执行后，仍有页面没有成功结果。"""

    def __init__(
        self,
        reference_path: Path,
        page_count: int,
        completed_page_numbers: list[int],
        page_errors: dict[int, str],
        *,
        fallback_page_models: dict[int, str] | None = None,
    ) -> None:
        self.fallback_page_models = dict(fallback_page_models or {})
        self.reference_path = reference_path
        self.page_count = page_count
        self.completed_page_numbers = tuple(sorted(completed_page_numbers))
        self.page_errors = dict(sorted(page_errors.items()))
        failed_pages = "、".join(str(page_number) for page_number in self.page_errors)
        super().__init__(
            f"Codex API OCR 仍有 {len(self.page_errors)} 页未完成: {reference_path.name} | 页码 {failed_pages}"
        )

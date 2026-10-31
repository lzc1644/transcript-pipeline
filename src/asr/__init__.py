"""Local ASR capabilities; model dependencies are loaded only during use."""
from src.asr.options import AsrResources, FileAsrOptions
from src.asr.results import AsrBatchItem, AsrFileResult, AsrOutputPaths, AsrSegmentResult

__all__ = ["AsrResources", "FileAsrOptions", "AsrBatchItem", "AsrFileResult",
           "AsrOutputPaths", "AsrSegmentResult"]

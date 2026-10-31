"""Stable ASR exceptions, independent of configuration and model runtimes."""

class AsrTranscriptionError(RuntimeError):
    """Raised when transcription fails."""


class AsrDependencyError(AsrTranscriptionError):
    """Raised when required ASR dependencies are missing."""


class UnsupportedAsrEngineError(AsrTranscriptionError):
    """Raised when the configured ASR engine is not supported."""


class InvalidAsrDeviceError(AsrTranscriptionError):
    """Raised when the configured ASR device is invalid."""


class CudaUnavailableError(AsrTranscriptionError):
    """Raised when CUDA is requested but unavailable."""


class AsrModelLoadError(AsrTranscriptionError):
    """Raised when the ASR model cannot be loaded."""


class AudioInputEmptyError(AsrTranscriptionError):
    """Raised when there are no supported audio files to transcribe."""

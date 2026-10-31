"""Compatibility exports; acoustic operations belong to ASR/media."""
from src.asr.streaming_audio import FRAME, RATE, SegmentConfig, Segmenter, SileroVAD
from src.media.audio import decode_audio_file as decode_file

__all__ = ["FRAME", "RATE", "SegmentConfig", "Segmenter", "SileroVAD", "decode_file"]

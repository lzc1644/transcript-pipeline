"""Legacy stage configuration adapter for pure ASR evidence inputs."""
from pathlib import Path
from src.llm.tasks.inputs import DualAsrInput, DualAsrInputError
from src.llm.tasks.inputs import load_dual_asr_input as _load_dual_asr_input
from src.schemas import LoadedSettings


def load_dual_asr_input(loaded: LoadedSettings, asr_text_path: Path) -> DualAsrInput | None:
    return _load_dual_asr_input(asr_text_path,
                               primary_candidate=loaded.settings.asr.candidate or "whisper-existing",
                               secondary_candidate=loaded.settings.asr.secondary_candidate)

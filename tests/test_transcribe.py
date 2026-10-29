from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.asr_utils import (
    AudioInputEmptyError,
    CudaUnavailableError,
    build_asr_output_paths,
    build_cuda_runtime_fix_hint,
    configure_cuda_runtime_from_venv,
    discover_cuda_runtime_library_dirs,
    find_python_package_dirs,
    iter_audio_files,
    resolve_cached_faster_whisper_model_path,
    load_faster_whisper_model,
    transcribe_audio_file,
    transcribe_batch,
)
from src.config_loader import load_settings
from tests.helpers import write_minimal_settings


def test_transcribe_batch_raises_when_audio_dir_empty(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    (tmp_path / "data/input/audio").mkdir(parents=True, exist_ok=True)

    loaded_settings = load_settings(project_root=tmp_path)

    with pytest.raises(AudioInputEmptyError):
        transcribe_batch(loaded_settings)


def test_iter_audio_files_filters_supported_extensions(tmp_path: Path) -> None:
    (tmp_path / "chapter01.wav").write_text("placeholder", encoding="utf-8")
    (tmp_path / "chapter02.MP3").write_text("placeholder", encoding="utf-8")
    (tmp_path / "chapter03.m4a").write_text("placeholder", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("placeholder", encoding="utf-8")

    files = iter_audio_files(tmp_path, [".wav", ".mp3", ".m4a", ".flac"])

    assert [path.name for path in files] == ["chapter01.wav", "chapter02.MP3", "chapter03.m4a"]


def test_build_asr_output_paths_uses_audio_basename(tmp_path: Path) -> None:
    audio_path = tmp_path / "meeting-session.wav"
    output_dir = tmp_path / "data/intermediate/asr"

    output_paths = build_asr_output_paths(audio_path, output_dir)

    assert output_paths.json_path == output_dir / "meeting-session.json"
    assert output_paths.txt_path == output_dir / "meeting-session.txt"


def test_transcribe_audio_file_prefers_profile_beam_size(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_text("placeholder", encoding="utf-8")

    loaded_settings = load_settings(project_root=tmp_path)
    loaded_settings.active_profile.beam_size = 9

    calls: dict[str, object] = {}

    class FakeModel:
        def transcribe(self, source: str, **kwargs):
            calls["source"] = source
            calls["kwargs"] = kwargs
            return iter([SimpleNamespace(id=0, start=0.0, end=1.0, text="测试")]), SimpleNamespace(language="zh")

    result = transcribe_audio_file(audio_path, FakeModel(), loaded_settings)

    assert result.full_text == "测试"
    assert calls["kwargs"]["beam_size"] == 9


def test_discover_cuda_runtime_library_dirs_supports_namespace_packages(monkeypatch) -> None:
    class FakeSpec:
        def __init__(self, directories: list[str]) -> None:
            self.origin = None
            self.submodule_search_locations = directories

    specs = {
        "nvidia.cublas.lib": FakeSpec(["/tmp/cublas"]),
        "nvidia.cudnn.lib": FakeSpec(["/tmp/cudnn"]),
    }

    monkeypatch.setattr(
        "src.asr.file.importlib.util.find_spec",
        lambda name: specs.get(name),
    )
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in {"/tmp/cublas", "/tmp/cudnn"})

    discovered = discover_cuda_runtime_library_dirs()

    assert discovered == [Path("/tmp/cublas"), Path("/tmp/cudnn")]


def test_find_python_package_dirs_returns_empty_when_parent_namespace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_find_spec(_name: str):
        raise ModuleNotFoundError("No module named 'nvidia'")

    monkeypatch.setattr("src.asr.file.importlib.util.find_spec", fake_find_spec)

    assert find_python_package_dirs("nvidia.cublas.lib") == []


def test_configure_cuda_runtime_from_venv_prepends_env_and_preloads_libs(tmp_path: Path, monkeypatch) -> None:
    cublas_dir = tmp_path / "cublas"
    cudnn_dir = tmp_path / "cudnn"
    cublas_dir.mkdir()
    cudnn_dir.mkdir()
    for filename in ("libcublas.so.12", "libcublasLt.so.12"):
        (cublas_dir / filename).write_text("", encoding="utf-8")
    (cudnn_dir / "libcudnn.so.9").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "src.asr.file.discover_cuda_runtime_library_dirs",
        lambda: [cublas_dir, cudnn_dir],
    )
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/lib/wsl/lib")

    loaded_paths: list[str] = []

    class FakeCtypes:
        RTLD_GLOBAL = 0

        @staticmethod
        def CDLL(path: str, mode: int) -> None:
            _ = mode
            loaded_paths.append(path)

    monkeypatch.setattr("src.asr.file.ctypes", FakeCtypes)

    configured_dirs = configure_cuda_runtime_from_venv()

    assert configured_dirs == [cublas_dir, cudnn_dir]
    assert os.environ["LD_LIBRARY_PATH"] == f"{cublas_dir}:{cudnn_dir}:/usr/lib/wsl/lib"
    assert loaded_paths == [
        str(cublas_dir / "libcublas.so.12"),
        str(cublas_dir / "libcublasLt.so.12"),
        str(cudnn_dir / "libcudnn.so.9"),
    ]


def test_build_cuda_runtime_fix_hint_mentions_export_when_runtime_dirs_exist(tmp_path: Path, monkeypatch) -> None:
    cublas_dir = tmp_path / "cublas"
    cudnn_dir = tmp_path / "cudnn"

    monkeypatch.setattr(
        "src.asr.file.discover_cuda_runtime_library_dirs",
        lambda: [cublas_dir, cudnn_dir],
    )

    hint = build_cuda_runtime_fix_hint()

    assert "LD_LIBRARY_PATH" in hint
    assert str(cublas_dir) in hint
    assert str(cudnn_dir) in hint


def test_load_faster_whisper_model_surfaces_cuda_hint_on_library_error(tmp_path: Path, monkeypatch) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    loaded_settings.active_profile.device = "cuda"
    loaded_settings.active_profile.asr_compute_type = "float16"
    loaded_settings.active_profile.asr_model_size = "large-v3"

    monkeypatch.setattr("src.asr.file.configure_cuda_runtime_from_venv", lambda: [])
    monkeypatch.setattr(
        "src.asr.file.build_cuda_runtime_fix_hint",
        lambda: "HINT: export LD_LIBRARY_PATH=...",
    )

    class FakeWhisperModel:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")

    monkeypatch.setattr("src.asr.file.import_whisper_model_class", lambda: FakeWhisperModel)

    with pytest.raises(CudaUnavailableError) as exc_info:
        load_faster_whisper_model(loaded_settings)

    assert "HINT: export LD_LIBRARY_PATH=..." in str(exc_info.value)


def test_resolve_cached_faster_whisper_model_path_uses_local_files_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_path = tmp_path / "snapshots" / "cached-model"

    def fake_download_model(model_size: str, *, cache_dir: str, local_files_only: bool):
        assert model_size == "large-v3-turbo"
        assert cache_dir == str(tmp_path)
        assert local_files_only is True
        return str(expected_path)

    monkeypatch.setattr("src.asr.file.import_faster_whisper_download_model", lambda: fake_download_model)

    resolved = resolve_cached_faster_whisper_model_path("large-v3-turbo", tmp_path)

    assert resolved == expected_path


def test_load_faster_whisper_model_prefers_cached_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    cached_path = tmp_path / "cached-model"
    captured: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(self, model_ref: str, **kwargs) -> None:
            captured["model_ref"] = model_ref
            captured["kwargs"] = kwargs

    monkeypatch.setattr("src.asr.file.resolve_cached_faster_whisper_model_path", lambda *_args, **_kwargs: cached_path)
    monkeypatch.setattr("src.asr.file.import_whisper_model_class", lambda: FakeWhisperModel)
    monkeypatch.setattr("src.asr.file.build_cuda_runtime_fix_hint", lambda: (_ for _ in ()).throw(AssertionError("should not be called for cpu")))

    load_faster_whisper_model(loaded_settings)

    assert captured["model_ref"] == str(cached_path)
    assert "download_root" not in captured["kwargs"]

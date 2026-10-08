from pathlib import Path
import os
import subprocess

import pytest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy_docker_wsl2.sh"


def test_docker_deploy_script_has_safe_entrypoints() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    help_result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    assert "--check" in help_result.stdout
    assert "--yes" in help_result.stdout
    assert "trans" in help_result.stdout
    assert "ASR_BACKENDS" in help_result.stdout
    assert "healthy 不代表空闲" in help_result.stdout
    assert "TRANSCRIPT_PROFILE=wsl2_gpu_high_accuracy" in help_result.stdout

    unknown_result = subprocess.run(
        ["bash", str(SCRIPT), "--unknown"], capture_output=True, text=True, check=False
    )
    assert unknown_result.returncode != 0
    assert "未知参数" in unknown_result.stderr


def test_compose_uses_only_trans_service_and_preserves_gpu_volume() -> None:
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert list(config["services"]) == ["trans"]
    service = config["services"]["trans"]
    assert service["deploy"]["resources"]["reservations"]["devices"][0]["capabilities"] == ["gpu"]
    assert "./data:/app/data" in service["volumes"]
    assert "model-cache:/home/app/.cache/transcript-pipeline" in service["volumes"]
    assert service["build"]["args"]["ASR_BACKENDS"] == "${ASR_BACKENDS:-qwen}"


def test_deploy_rejects_invalid_asr_backend_before_host_changes() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"], capture_output=True, text=True,
        env={**os.environ, "ASR_BACKENDS": "invalid"},
    )
    assert result.returncode != 0
    assert "ASR_BACKENDS 必须是" in result.stderr


@pytest.mark.parametrize("backend", ["", "whisper", "qwen", "funasr", "all"])
@pytest.mark.parametrize("use_sudo", [0, 1])
def test_deploy_passes_asr_backend_through_compose_wrapper(backend: str, use_sudo: int) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    # Execute the actual initialization and wrapper, without any host operations.
    initialization = source.split('export ASR_BACKENDS=', 1)[1].split('esac', 1)[0]
    wrapper = source.split('dc() {', 1)[1].split('\n}', 1)[0]
    shell = 'export ASR_BACKENDS=' + initialization + 'esac\n'
    shell += '''
fail() { exit 2; }
sudo() { (
    unset ASR_BACKENDS
    test "$1" = env
    shift
    while [[ "$1" == *=* ]]; do export "$1"; shift; done
    "$@"
); }
docker() { printf '%s\\n' "$ASR_BACKENDS" "$@"; }
'''
    shell += f'USE_SUDO={use_sudo}\nAPP_UID=1000\nAPP_GID=1000\n'
    shell += 'dc() {' + wrapper + '\n}\ndc build trans\n'
    result = subprocess.run(
        ["bash", "-eu", "-c", shell], capture_output=True, text=True, check=True,
        env={**os.environ, "ASR_BACKENDS": backend},
    )
    assert result.stdout.splitlines() == [backend or "qwen", "compose", "build", "trans"]


@pytest.mark.parametrize("profile", ["", "wsl2_gpu", "custom_gpu"])
@pytest.mark.parametrize("use_sudo", [0, 1])
def test_wsl_deploy_profile_default_and_override_match_sudo(profile: str, use_sudo: int) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    initialization = source.split('export TRANSCRIPT_PROFILE=', 1)[1].split('\n', 1)[0]
    wrapper = source.split('dc() {', 1)[1].split('\n}', 1)[0]
    shell = '''
sudo() { (
    unset TRANSCRIPT_PROFILE
    test "$1" = env
    shift
    while [[ "$1" == *=* ]]; do export "$1"; shift; done
    "$@"
); }
# Mimic Compose precedence: shell overrides a local_cpu value from .env.
docker() { printf '%s\\n' "${TRANSCRIPT_PROFILE:-local_cpu}" "$@"; }
'''
    shell += 'export TRANSCRIPT_PROFILE=' + initialization + '\n'
    shell += f'USE_SUDO={use_sudo}\nAPP_UID=1000\nAPP_GID=1000\nASR_BACKENDS=qwen\n'
    shell += 'dc() {' + wrapper + '\n}\ndc build trans\n'
    result = subprocess.run(
        ["bash", "-eu", "-c", shell], capture_output=True, text=True, check=True,
        env={**os.environ, "TRANSCRIPT_PROFILE": profile},
    )
    assert result.stdout.splitlines() == [profile or "wsl2_gpu_high_accuracy", "compose", "build", "trans"]


def test_wsl_build_failure_does_not_recreate_running_service() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    build_and_start = source.split('\ndc build trans', 1)[1].split('\ncontainer=', 1)[0]
    shell = '''
fail() { printf '%s\\n' "$*" >&2; exit 1; }
dc() { printf '%s\\n' "$*"; test "$1" != build; }
'''
    result = subprocess.run(
        ["bash", "-eu", "-c", shell + '\ndc build trans' + build_and_start],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "镜像构建失败" in result.stderr
    assert result.stdout.splitlines() == ["build trans"]


def test_optional_asr_constraints_allow_compatible_ruff_patch_releases() -> None:
    from packaging.specifiers import SpecifierSet

    lines = (ROOT / "requirements-asr-constraints.txt").read_text(encoding="utf-8").splitlines()
    ruff = next(line for line in lines if line.startswith("ruff"))
    allowed = SpecifierSet(ruff.removeprefix("ruff"))
    assert all(version in allowed for version in ("0.9.3", "0.16.8", "0.16.9", "0.16.10"))
    assert "0.9.2" not in allowed and "0.17.0" not in allowed
    # Keep inference dependencies locked; only Gradio's tooling is relaxed.
    assert "qwen-asr==0.0.6" in lines and "torch==2.8.0" in lines
    for name in ("requirements-asr-qwen.txt", "requirements-asr-funasr.txt"):
        assert "-c requirements-asr-constraints.txt" in (ROOT / name).read_text(encoding="utf-8")


def test_docker_optional_sdk_install_retries_network_without_skipping_validation() -> None:
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    sdk_installs = [line for line in source.splitlines() if "/asr-py312/bin/python -m pip install" in line]
    assert len(sdk_installs) == 3
    assert all("--retries 10 --timeout 60" in line for line in sdk_installs)
    assert "--resume-retries 10" in sdk_installs[-1]
    assert "/app/.venv/asr-py312/bin/python -m pip check" in source
    assert "--no-deps" not in source and "--trusted-host" not in source

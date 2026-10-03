"""Exercise the complete entrypoint with command stubs; never touch host services."""
from pathlib import Path
import json
import os
import pty
import shutil
import socket
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy_docker_linux.sh"
KNOBS = ("APP_UID", "APP_GID", "APP_PORT", "DOCKER_BIND_IP", "TRANSCRIPT_PROFILE", "ASR_BACKENDS")

# Each stub records calls. sudo scrubs deployment knobs like the real default
# sudo policy; only the explicit env assignments can get them to Docker.
STUB = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
base = Path(os.environ["STUB_BASE"])
state_path = base / "state.json"
s = json.loads(state_path.read_text())
with (base / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"name": name, "args": args,
        "sudo": os.environ.get("STUB_SUDO", "0"),
        "knobs": {k: os.environ.get(k) for k in
        ("APP_UID", "APP_GID", "APP_PORT", "DOCKER_BIND_IP", "TRANSCRIPT_PROFILE", "ASR_BACKENDS")}}) + "\n")
def save(): state_path.write_text(json.dumps(s))
def tool(name):
    path = base / "bin" / name
    if not path.exists(): path.symlink_to(base / "stub")
def die(): sys.exit(1)
if name == "sudo":
    assert args[0] in ("env", "apt-get", "systemctl", "gpg", "install", "tee", "cp") or args[0].endswith("nvidia-ctk"), args
    env = {k: v for k, v in os.environ.items() if k not in
        ("APP_UID", "APP_GID", "APP_PORT", "DOCKER_BIND_IP", "TRANSCRIPT_PROFILE", "ASR_BACKENDS",
         "DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")}
    env["STUB_SUDO"] = "1"
    # Root's default context intentionally points elsewhere. --host must win.
    env["STUB_ROOT_CONTEXT"] = "remote-root"
    os.execvpe(args[0], args, env)
elif name == "uname": print(s["arch"] if args == ["-m"] else s["kernel"])
elif name == "ps": print(s["pid1"])
elif name == "nvidia-smi":
    if not s["driver"]: die()
    print("GPU 0: NVIDIA test GPU")
elif name == "dpkg-query":
    if args[-1] not in s["packages"]: die()
    print("install ok installed", end="")
elif name == "sleep": pass
elif name == "systemctl":
    if args[0] == "show": print(s["engine_unit"])
    elif args[0] == "is-active":
        if not s["daemon"]: die()
    else:
        assert args[0] in ("start", "restart", "enable")
        s["daemon"] = True
        save()
elif name == "apt-get":
    assert "upgrade" not in args and "remove" not in args
    if "install" in args:
        assert "--no-remove" in args and "--no-upgrade" in args
        if "docker-ce" in args:
            tool("docker")
            s["compose"] = True
            s["packages"].append("docker-ce")
            s["engine_unit"] = "loaded"
        if "docker-compose-plugin" in args: s["compose"] = True
        if "nvidia-container-toolkit" in args:
            tool("nvidia-ctk")
            s["packages"].append("nvidia-container-toolkit")
        save()
elif name == "nvidia-ctk":
    assert args == ["runtime", "configure", "--runtime=docker"]
    s["runtime"] = True
    save()
elif name == "curl":
    if s.get("repo_fail") and any("/resolute/Release" in a for a in args): die()
    output = Path(args[args.index("-o") + 1])
    output.write_text("deb https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /\n")
elif name in ("gpg", "install", "tee", "cp"):
    # Do not run any real privileged filesystem commands.
    if name == "tee":
        assert args[0].startswith(str(base))
        Path(args[0]).parent.mkdir(parents=True, exist_ok=True)
        Path(args[0]).write_text(sys.stdin.read())
    elif name == "cp":
        assert args[-1].startswith(str(base))
        Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
elif name == "docker":
    if args[:2] == ["context", "show"]:
        print(s["context"])
        sys.exit(0)
    if args[:2] == ["context", "inspect"]:
        print(s["endpoint"] if args[2] == s["context"] else s.get("override_endpoint", s["endpoint"]))
        sys.exit(0)
    assert args[:2] == ["--host", "unix:///var/run/docker.sock"], args
    args = args[2:]
    if args[0] == "compose":
        if not s["compose"]: die()
        if args[1] == "version": print("v5.6.0"); sys.exit(0)
    if args == ["info"] and not os.environ.get("STUB_SUDO"):
        s["plain_info_calls"] = s.get("plain_info_calls", 0) + 1
        save()
        if s.get("deny_after_preflight") and s["plain_info_calls"] > 1: die()
    if not s["daemon"] or (not s["access"] and not os.environ.get("STUB_SUDO")): die()
    if args[0] == "info":
        fmt = args[-1]
        if ".ID" in fmt:
            identity = s["id"]
            if os.environ.get("STUB_SUDO"): identity = s.get("sudo_id", identity)
            if s.get("change_id") and s["runtime"]: identity = "other-daemon"
            print(identity + "|" + s["os"] + "|" + s["security"])
        elif ".Runtimes" in fmt: print('{"nvidia":{}}' if s["runtime"] else '{"runc":{}}')
    elif args[0] == "ps":
        key = "legacy" if "label=com.docker.compose.service=app" in args else "trans"
        if s.get("ps_fail"): die()
        print(s[key])
    elif args[0] == "run":
        assert "--gpus" in args and "all" in args and "--rm" in args
        if s.get("gpu_fail"): die()
    elif args[0] == "inspect": print(s["health"])
    elif args[0] == "compose":
        assert args[1] == "--project-directory" and args[3:5] == ["--project-name", "transcript-pipeline"]
        assert args[5] == "-f"
        operation = args[7:]
        if operation[0] == "config": assert operation == ["config", "--quiet"]
        if operation[0] == "build" and s.get("build_fail"): die()
        if operation[0] == "up" and s.get("up_fail"): die()
        if operation[:3] == ["ps", "-q", "trans"]: print("container-test")
        if operation[0] == "exec" and s.get("ct2_fail"): die()
    else: raise AssertionError(args)
else: raise AssertionError(name)
'''


class Host:
    def __init__(self, base: Path):
        self.base = base
        self.state = dict(
            kernel="Linux 7.0.0-generic", arch="x86_64", pid1="systemd", driver=True,
            context="default", endpoint="unix:///var/run/docker.sock", daemon=True,
            access=True, compose=True, runtime=True, engine_unit="loaded", id="local-daemon", os="Ubuntu 26.04.1 LTS",
            security='["name=seccomp"]', legacy="", trans="", health="healthy", packages=["docker-ce"],
        )
        bindir = base / "bin"
        bindir.mkdir()
        (base / "stub").write_text(f"#!{sys.executable}\n" + STUB)
        (base / "stub").chmod(0o755)
        for name in ("docker", "sudo", "systemctl", "uname", "ps", "nvidia-smi", "dpkg-query",
                     "apt-get", "nvidia-ctk", "curl", "gpg", "install", "tee", "cp", "sleep"):
            (bindir / name).symlink_to(base / "stub")
        # Deliberately omit the real docker/apt/sudo from PATH.
        for name in ("bash", "cat", "dirname", "env", "id", "stat", "grep", "mktemp", "rm", "sed", "date"):
            (bindir / name).symlink_to(shutil.which(name))
        repo = base / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "data").mkdir()
        for name in ("Dockerfile", "compose.yaml"):
            shutil.copyfile(ROOT / name, repo / name)
        self.os_release = base / "os-release"
        self.os_release.write_text('ID=ubuntu\nVERSION_ID=26.04\nVERSION_CODENAME=resolute\n')
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(base / "docker.sock"))
        source = SCRIPT.read_text().replace("source /etc/os-release", f'source "{self.os_release}"')
        source = source.replace("SOCKET_PATH=/var/run/docker.sock", f'SOCKET_PATH="{base / "docker.sock"}"')
        # Test copy only: isolate host configuration paths, not endpoint strings.
        source = source.replace("/etc/apt/", str(base / "etc/apt") + "/")
        source = source.replace("/etc/docker/", str(base / "etc/docker") + "/")
        source = source.replace("/usr/share/keyrings/", str(base / "keyrings") + "/")
        for path in ("/usr/bin/nvidia-ctk", "/usr/sbin/nvidia-ctk", "/usr/local/bin/nvidia-ctk"):
            source = source.replace(path, str(base / "absent-ctk"))
        self.script = repo / "scripts/deploy_docker_linux.sh"
        self.script.write_text(source)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("DOCKER_", "COMPOSE_")) and k not in KNOBS}
        self.env.update(PATH=str(bindir), STUB_BASE=str(base))

    def run(self, *args: str, **knobs: str):
        (self.base / "state.json").write_text(json.dumps(self.state))
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True,
                              env={**self.env, **knobs}, timeout=20)

    def calls(self):
        path = self.base / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def hide(self, name: str):
        (self.base / "bin" / name).unlink()


@pytest.fixture
def host(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("Deployment entrypoint requires an ordinary user")
    h = Host(tmp_path)
    yield h
    h.sock.close()


def assert_read_only(host):
    for c in host.calls():
        assert c["name"] not in ("sudo", "apt-get", "curl", "gpg", "install", "tee", "cp", "nvidia-ctk")
        if c["name"] == "systemctl": assert c["args"][0] in ("is-active", "show")
        if c["name"] == "docker":
            args = c["args"]
            if args[:1] == ["--host"]: args = args[2:]
            assert args[0] in ("context", "info", "ps", "compose")
            if args[0] == "compose": assert args[1] == "version"
    assert not (host.base / "etc").exists()
    assert list((host.base / "repo/data").iterdir()) == []


def test_help_and_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    assert all(word in result.stdout for word in ("--check", "--yes", "DEPLOY", "ASR_BACKENDS", "退出码"))


@pytest.mark.parametrize("args,env,message", [
    (("--unknown",), {}, "未知参数"),
    (("--check", "--yes"), {}, "不能同时使用"),
    (("--check",), {"ASR_BACKENDS": "invalid"}, "ASR_BACKENDS 必须是"),
    (("--check",), {"APP_UID": "0"}, "非 root"),
    (("--check",), {"APP_GID": "000"}, "非 root"),
    (("--check",), {"APP_PORT": "70000"}, "APP_PORT"),
])
def test_invalid_options(host, args, env, message):
    result = host.run(*args, **env)
    assert result.returncode == 1 and message in result.stderr
    assert_read_only(host)


@pytest.mark.parametrize("field,value,message", [
    ("kernel", "Linux 6.6-microsoft-standard-WSL2", "deploy_docker_wsl2.sh"),
    ("arch", "aarch64", "x86_64"), ("pid1", "init", "systemd"),
    ("driver", False, "绝不安装或替换驱动"),
    ("endpoint", "tcp://remote:2375", "非本机"),
    ("endpoint", "unix:///run/user/1000/docker.sock", "rootless"),
    ("endpoint", "unix:///home/me/.docker/desktop/docker.sock", "Desktop"),
    ("os", "Docker Desktop", "实际 daemon"),
    ("security", '["name=rootless"]', "rootless"),
    ("legacy", "transcript-pipeline-app-1 Up healthy", "旧 app"),
])
def test_explicit_blockers(host, field, value, message):
    host.state[field] = value
    result = host.run("--check")
    assert result.returncode == 1 and message in result.stderr
    assert_read_only(host)


@pytest.mark.parametrize("release", ["ID=debian\n", "ID=ubuntu\nVERSION_ID=25.10\nVERSION_CODENAME=questing\n"])
def test_unsupported_os(host, release):
    host.os_release.write_text(release)
    assert host.run("--check").returncode == 1
    assert_read_only(host)


def test_socket_denied_is_incomplete_not_missing_engine(host):
    host.state["access"] = False
    (host.base / "docker.sock").chmod(0o400)
    host.hide("nvidia-ctk")
    result = host.run("--check")
    assert result.returncode == 2
    assert "服务已运行，但当前用户无权访问" in result.stdout
    assert "daemon、runtime、已有容器检查未完成" in result.stdout
    assert "预检通过" not in result.stdout and "Docker CLI 不存在" not in result.stdout
    assert_read_only(host)


def test_ready_check_and_existing_trans_warning(host):
    host.state["trans"] = "transcript-pipeline-trans-1 Up healthy"
    result = host.run("--check")
    assert result.returncode == 0
    assert "须先确认任务结束" in result.stdout
    assert "healthy 不代表" in result.stdout
    assert_read_only(host)


@pytest.mark.parametrize("missing", ["compose", "runtime", "toolkit", "docker"])
def test_missing_dependency_is_not_pass(host, missing):
    if missing in ("compose", "runtime"): host.state[missing] = False
    else: host.hide("nvidia-ctk" if missing == "toolkit" else "docker")
    if missing == "docker": host.state["daemon"] = False
    result = host.run("--check")
    assert result.returncode == 1 and "预检通过" not in result.stdout
    assert_read_only(host)


def test_stopped_daemon_and_different_uid_are_incomplete(host):
    host.state["daemon"] = False
    result = host.run("--check", APP_UID="2000", APP_GID="2000")
    assert result.returncode == 2 and "检查未完成" in result.stdout
    assert_read_only(host)


def test_no_confirmation_means_no_changes(host):
    host.hide("nvidia-ctk")
    result = host.run()
    assert result.returncode == 1 and "未确认" in result.stderr
    assert_read_only(host)


@pytest.mark.parametrize("answer,expected", [("NO", 1), ("DEPLOY", 0)])
def test_interactive_confirmation(host, answer, expected):
    (host.base / "state.json").write_text(json.dumps(host.state))
    master, slave = pty.openpty()
    process = subprocess.Popen(["bash", str(host.script)], stdin=slave, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=host.env, text=True)
    os.close(slave)
    try:
        os.write(master, (answer + "\n").encode())
        stdout, stderr = process.communicate(timeout=20)
    finally:
        os.close(master)
        if process.poll() is None: process.kill()
    assert process.returncode == expected, stdout + stderr
    assert "DEPLOY" in stdout
    if expected == 1:
        assert "未确认" in stderr
        assert_read_only(host)
    else:
        assert any(c["name"] == "docker" and "build" in c["args"] for c in host.calls())


@pytest.mark.parametrize("sudo", [False, True])
@pytest.mark.parametrize("backend", ["", "whisper", "qwen", "funasr", "all"])
def test_deploy_wrapper_preserves_all_knobs_and_pins_daemon(host, sudo, backend):
    host.state["access"] = not sudo
    if sudo: (host.base / "docker.sock").chmod(0o400)
    env = dict(APP_UID="2000", APP_GID="2001", APP_PORT="8181",
               DOCKER_BIND_IP="127.0.0.1", TRANSCRIPT_PROFILE="wsl2_gpu", ASR_BACKENDS=backend,
               DOCKER_CONTEXT="local", DOCKER_HOST="tcp://ignored-remote:2375",
               COMPOSE_FILE="evil.yaml", COMPOSE_PROJECT_NAME="other-project")
    result = host.run("--yes", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.calls()
    build = next(c for c in calls if c["name"] == "docker" and c["args"][-2:] == ["build", "trans"])
    expected = {k: env[k] for k in KNOBS}
    expected["ASR_BACKENDS"] = backend or "qwen"
    assert build["knobs"] == expected
    assert build["sudo"] == str(int(sudo))
    assert build["args"][:2] == ["--host", "unix:///var/run/docker.sock"]
    assert not any(c["name"] in ("apt-get", "nvidia-ctk", "systemctl") and c["args"][0] not in ("is-active", "show") for c in calls)


def test_docker_host_and_context_precedence(host):
    assert host.run("--check", DOCKER_HOST="tcp://remote:2375").returncode == 1
    host.state["override_endpoint"] = "tcp://remote:2375"
    assert host.run("--check", DOCKER_CONTEXT="remote", DOCKER_HOST="unix:///var/run/docker.sock").returncode == 1
    assert_read_only(host)


def test_legacy_discovered_via_sudo_stops_before_changes(host):
    host.state.update(access=False, legacy="legacy-app", runtime=False)
    (host.base / "docker.sock").chmod(0o400)
    host.hide("nvidia-ctk")
    result = host.run("--yes")
    assert result.returncode == 1 and "旧 app" in result.stderr
    assert not any(c["name"] in ("apt-get", "nvidia-ctk", "curl") for c in host.calls())


def test_runtime_configuration_is_backed_up_and_restarted_once(host):
    host.state["runtime"] = False
    daemon = host.base / "etc/docker/daemon.json"
    daemon.parent.mkdir(parents=True)
    daemon.write_text('{"log-driver":"json-file"}')
    result = host.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.calls()
    assert sum(c["name"] == "nvidia-ctk" for c in calls) == 1
    assert sum(c["name"] == "systemctl" and c["args"] == ["restart", "docker"] for c in calls) == 1
    backup = list(daemon.parent.glob("daemon.json.before-transcript-*"))
    assert len(backup) == 1 and backup[0].read_text() == daemon.read_text()


@pytest.mark.parametrize("failure,message", [
    ("gpu_fail", "Docker GPU 检查失败"), ("build_fail", "镜像构建失败"),
    ("up_fail", "启动失败"), ("ct2_fail", "运行库检查失败"),
    ("ps_fail", "服务检查失败"), ("change_id", "daemon ID 改变"),
])
def test_deploy_failure_is_explicit(host, failure, message):
    host.state[failure] = True
    if failure == "change_id": host.state["runtime"] = False
    result = host.run("--yes")
    assert result.returncode == 1 and message in result.stderr
    assert not any("local_cpu" in c["args"] for c in host.calls())


@pytest.mark.parametrize("health", ["unhealthy", "no-healthcheck", "starting"])
def test_health_failure(host, health):
    host.state["health"] = health
    result = host.run("--yes")
    assert result.returncode == 1 and "健康检查未通过" in result.stderr
    assert not any("exec" in c["args"] for c in host.calls())


def test_sudo_daemon_identity_change_is_blocked(host):
    host.state.update(deny_after_preflight=True, sudo_id="other-daemon")
    result = host.run("--yes")
    assert result.returncode == 1 and "daemon ID 改变" in result.stderr
    assert not any(c["name"] == "docker" and "build" in c["args"] for c in host.calls())


def test_cli_only_host_can_install_missing_engine(host):
    host.state.update(engine_unit="not-found", packages=["docker-ce-cli"], daemon=False)
    result = host.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(c["name"] == "apt-get" and "docker-ce" in c["args"] for c in host.calls())


def test_accessible_daemon_without_systemd_service_is_not_replaced(host):
    host.state["engine_unit"] = "not-found"
    result = host.run("--yes")
    assert result.returncode == 1 and "不安装第二套 Engine" in result.stderr
    assert_read_only(host)


@pytest.mark.parametrize("mode", ["missing", "unwritable"])
def test_data_permissions_are_not_automatically_fixed(host, mode):
    data = host.base / "repo/data"
    if mode == "missing": data.rmdir()
    else: data.chmod(0o500)
    try:
        result = host.run("--yes")
        assert result.returncode == 1 and "不自动 chown" in result.stderr
        assert not any(c["name"] == "sudo" for c in host.calls())
    finally:
        if data.exists(): data.chmod(0o700)


def test_missing_driver_command(host):
    host.hide("nvidia-smi")
    assert host.run("--yes").returncode == 1
    assert_read_only(host)


def test_cli_missing_with_active_service_is_incomplete(host):
    host.hide("docker")
    assert host.run("--check").returncode == 2
    assert_read_only(host)
    result = host.run("--yes")
    assert result.returncode == 1
    assert not any(c["name"] == "apt-get" for c in host.calls())


def test_compose_install_on_existing_official_engine(host):
    host.state["compose"] = False
    result = host.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    installs = [c["args"] for c in host.calls() if c["name"] == "apt-get" and "install" in c["args"]]
    assert len(installs) == 1 and "docker-compose-plugin" in installs[0]
    assert "docker-ce" not in installs[0] and "containerd.io" not in installs[0]


def test_env_example_does_not_override_script_gpu_default(host):
    (host.base / "repo/.env").write_text("TRANSCRIPT_PROFILE=local_cpu\n")
    result = host.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    build = next(c for c in host.calls() if c["name"] == "docker" and "build" in c["args"])
    assert build["knobs"]["TRANSCRIPT_PROFILE"] == "wsl2_gpu_high_accuracy"


def test_toolkit_package_without_executable_is_not_reinstalled(host):
    host.hide("nvidia-ctk")
    host.state["packages"].append("nvidia-container-toolkit")
    assert host.run("--check").returncode == 2
    result = host.run("--yes")
    assert result.returncode == 1 and "人工修复路径" in result.stderr
    assert not any(c["name"] == "apt-get" for c in host.calls())


def test_fresh_install_uses_resolute_and_never_removes_packages(host):
    host.hide("docker")
    host.hide("nvidia-ctk")
    host.state.update(packages=[], daemon=False, runtime=False)
    result = host.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    source = (host.base / "etc/apt/sources.list.d/docker.sources").read_text()
    assert "Suites: resolute" in source and "noble" not in source
    calls = host.calls()
    assert any(c["name"] == "curl" and any("/resolute/Release" in a for a in c["args"]) for c in calls)


def test_missing_release_does_not_fallback(host):
    host.hide("docker")
    host.state.update(packages=[], daemon=False, repo_fail=True)
    result = host.run("--yes")
    assert result.returncode == 1 and "不替换代号" in result.stderr
    assert not (host.base / "etc").exists()
    assert not any(c["name"] == "apt-get" for c in host.calls())


@pytest.mark.parametrize("package", ["docker.io", "containerd", "runc"])
def test_conflicting_packages_are_left_for_operator(host, package):
    host.hide("docker")
    host.state.update(packages=[package], daemon=False)
    result = host.run("--yes")
    assert result.returncode == 1 and "不自动卸载/替换" in result.stderr
    assert not any(c["name"] == "apt-get" for c in host.calls())


def test_compose_missing_on_distro_engine_does_not_replace_it(host):
    host.state.update(packages=["docker.io"], compose=False)
    result = host.run("--yes")
    assert result.returncode == 1 and "原安装来源" in result.stderr
    assert not any(c["name"] == "apt-get" for c in host.calls())


def test_source_has_no_data_deletion_driver_or_firewall_operations():
    source = SCRIPT.read_text()
    assert not re.search(r"^\s*(?:sudo )?(?:usermod|chown|ufw|iptables|nvidia-installer)\s", source, re.MULTILINE)
    assert "apt-get upgrade" not in source and "apt-get remove" not in source
    assert not re.search(r"^\s*(?:dc|dkr) .*?(?:down -v|--remove-orphans)", source, re.MULTILINE)
    assert 'trap \'rm -rf "$tmp_dir"\' EXIT' in source
    assert "apt_install nvidia-container-toolkit" in source
    assert "nvidia-driver" not in source and "cuda-toolkit" not in source

import subprocess
from pathlib import Path

from nipux_cli.uninstall import build_uninstall_plan, installed_tool_paths, uninstall_installed_tool, uninstall_runtime


def _completed(*_args, **_kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0)


def test_uninstall_plan_includes_runtime_and_legacy_state(monkeypatch, tmp_path):
    home = tmp_path / "user"
    profile = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NIPUX_HOME", str(profile))

    plan = build_uninstall_plan()

    assert profile in plan.paths
    assert home / ".nipux" in plan.paths
    assert home / ".kneepucks" in plan.paths
    assert home / "Library" / "LaunchAgents" / "com.nipux.agent.plist" in plan.service_paths
    assert home / ".config" / "systemd" / "user" / "nipux.service" in plan.service_paths


def test_uninstall_plan_includes_configured_runtime_home(monkeypatch, tmp_path):
    home = tmp_path / "user"
    profile = tmp_path / "profile"
    configured = tmp_path / "configured"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NIPUX_HOME", str(profile))

    plan = build_uninstall_plan(runtime_home=configured)

    assert configured in plan.paths
    assert profile in plan.paths


def test_uninstall_runtime_removes_state_and_service_files(monkeypatch, tmp_path):
    home = tmp_path / "user"
    profile = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NIPUX_HOME", str(profile))

    paths = [
        profile,
        home / ".nipux",
        home / ".kneepucks",
        home / "Library" / "LaunchAgents",
        home / ".config" / "systemd" / "user",
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    (home / "Library" / "LaunchAgents" / "com.nipux.agent.plist").write_text("plist", encoding="utf-8")
    (home / ".config" / "systemd" / "user" / "nipux.service").write_text("unit", encoding="utf-8")
    (profile / "state.db").write_text("state", encoding="utf-8")

    lines = uninstall_runtime(runner=_completed)

    assert any("removed" in line and str(profile) in line for line in lines)
    assert not profile.exists()
    assert not (home / ".nipux").exists()
    assert not (home / ".kneepucks").exists()
    assert not (home / "Library" / "LaunchAgents" / "com.nipux.agent.plist").exists()
    assert not (home / ".config" / "systemd" / "user" / "nipux.service").exists()


def test_uninstall_runtime_dry_run_keeps_files(monkeypatch, tmp_path):
    home = tmp_path / "user"
    profile = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NIPUX_HOME", str(profile))
    profile.mkdir(parents=True)

    lines = uninstall_runtime(dry_run=True, runner=_completed)

    assert any("would remove" in line and str(profile) in line for line in lines)
    assert profile.exists()


def test_uninstall_installed_tool_uses_uv_when_available(monkeypatch, tmp_path):
    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("nipux_cli.uninstall.shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    calls = []

    def runner(command):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="Uninstalled 1 executable: nipux\n")

    code, lines = uninstall_installed_tool(runner=runner)

    assert code == 0
    assert calls == [("/usr/bin/uv", "tool", "uninstall", "nipux")]
    assert "Uninstalled 1 executable: nipux" in "\n".join(lines)


def test_uninstall_installed_tool_falls_back_to_safe_uv_paths(monkeypatch, tmp_path):
    home = tmp_path / "user"
    shim = home / ".local" / "bin" / "nipux"
    tool_dir = home / ".local" / "share" / "uv" / "tools" / "nipux"
    tool_bin = tool_dir / "bin"
    tool_bin.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    target = tool_bin / "nipux"
    target.write_text("script", encoding="utf-8")
    shim.symlink_to(target)
    monkeypatch.setenv("HOME", str(home))

    def which(name):
        if name == "nipux":
            return str(shim)
        return None

    monkeypatch.setattr("nipux_cli.uninstall.shutil.which", which)

    code, lines = uninstall_installed_tool()

    assert code == 0
    assert not shim.exists()
    assert not tool_dir.exists()
    rendered = "\n".join(lines)
    assert "uv not found; checking safe local tool paths" in rendered
    assert f"removed {shim}" in rendered
    assert f"removed {tool_dir}" in rendered


def test_installed_tool_paths_ignore_non_user_tool(monkeypatch, tmp_path):
    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("nipux_cli.uninstall.shutil.which", lambda name: "/usr/local/bin/nipux" if name == "nipux" else None)

    paths = installed_tool_paths()

    assert Path("/usr/local/bin/nipux") not in paths
    assert home / ".local" / "bin" / "nipux" in paths

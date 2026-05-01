"""OS service installation helpers for the Nipux daemon."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

from nipux_cli.config import load_config


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.nipux.agent.plist"


def launch_agent_plist(*, poll_seconds: float, quiet: bool) -> str:
    config = load_config()
    config.ensure_dirs()
    command = [
        sys.executable,
        "-m",
        "nipux_cli.cli",
        "daemon",
        "--poll-seconds",
        str(poll_seconds),
    ]
    command.append("--quiet" if quiet else "--verbose")
    args_xml = "\n".join(f"        <string>{xml_escape(part)}</string>" for part in command)
    log_path = config.runtime.logs_dir / "launchd-daemon.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.nipux.agent</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>NIPUX_HOME</key>
      <string>{xml_escape(str(config.runtime.home))}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{xml_escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{xml_escape(str(log_path))}</string>
    <key>WorkingDirectory</key>
    <string>{xml_escape(str(Path.cwd()))}</string>
  </dict>
</plist>
"""


def systemd_service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "nipux.service"


def systemd_service_text(*, poll_seconds: float, quiet: bool) -> str:
    config = load_config()
    config.ensure_dirs()
    command = [
        sys.executable,
        "-m",
        "nipux_cli.cli",
        "daemon",
        "--poll-seconds",
        str(poll_seconds),
    ]
    command.append("--quiet" if quiet else "--verbose")
    return "\n".join(
        [
            "[Unit]",
            "Description=Nipux 24/7 autonomous worker",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={Path.cwd()}",
            f"Environment=NIPUX_HOME={config.runtime.home}",
            f"ExecStart={' '.join(shlex.quote(part) for part in command)}",
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def cmd_autostart(args: Namespace) -> None:
    path = launch_agent_path()
    label = "gui/" + str(os.getuid()) + "/com.nipux.agent"
    if args.action == "status":
        status = "installed" if path.exists() else "not installed"
        print(f"autostart: {status}")
        print(f"plist: {path}")
        if path.exists():
            result = subprocess.run(
                ["launchctl", "print", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print("launchd: loaded" if result.returncode == 0 else "launchd: not loaded")
        return
    if args.action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launch_agent_plist(poll_seconds=args.poll_seconds, quiet=args.quiet), encoding="utf-8")
        subprocess.run(
            ["launchctl", "bootout", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        result = subprocess.run(["launchctl", "bootstrap", "gui/" + str(os.getuid()), str(path)], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
        subprocess.run(["launchctl", "enable", label], check=False)
        print(f"autostart installed: {path}")
        print("daemon will start at login and launchd will keep it alive")
        return
    if args.action == "uninstall":
        subprocess.run(
            ["launchctl", "bootout", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if path.exists():
            path.unlink()
        print("autostart uninstalled")
        return
    raise SystemExit(f"unknown autostart action: {args.action}")


def cmd_service(args: Namespace) -> None:
    path = systemd_service_path()
    systemctl = shutil.which("systemctl")
    user_cmd = [systemctl, "--user"] if systemctl else None
    if args.action == "status":
        print(f"service: {'installed' if path.exists() else 'not installed'}")
        print(f"unit: {path}")
        if user_cmd:
            result = subprocess.run(
                [*user_cmd, "is-active", "nipux.service"], check=False, capture_output=True, text=True
            )
            print(f"systemd: {result.stdout.strip() or result.stderr.strip() or 'unknown'}")
        else:
            print("systemd: unavailable on this machine")
        return
    if args.action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_service_text(poll_seconds=args.poll_seconds, quiet=args.quiet), encoding="utf-8")
        print(f"service file written: {path}")
        if user_cmd:
            subprocess.run([*user_cmd, "daemon-reload"], check=False)
            subprocess.run([*user_cmd, "enable", "--now", "nipux.service"], check=False)
            print("systemd user service enabled and started")
        else:
            print(
                "systemd not found; copy this service to a Linux server or run: systemctl --user enable --now nipux.service"
            )
        return
    if args.action == "uninstall":
        if user_cmd:
            subprocess.run([*user_cmd, "disable", "--now", "nipux.service"], check=False)
            subprocess.run([*user_cmd, "daemon-reload"], check=False)
        if path.exists():
            path.unlink()
        print("service uninstalled")
        return
    raise SystemExit(f"unknown service action: {args.action}")


def xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

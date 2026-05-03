"""Help text and static branding for the Nipux command console."""

from __future__ import annotations

from typing import Callable


NIPUX_BANNER = r"""
 _   _ _                  ____ _     ___
| \ | (_)_ __  _   ___  _/ ___| |   |_ _|
|  \| | | '_ \| | | \ \/ / |   | |    | |
| |\  | | |_) | |_| |>  <| |___| |___ | |
|_| \_|_| .__/ \__,_/_/\_\\____|_____|___|
        |_|
""".strip("\n")


def print_shell_help(*, rule: Callable[[str], str]) -> None:
    print(NIPUX_BANNER)
    print(rule("="))
    _print_group(
        "Jobs",
        (
            'create "objective" --title TITLE',
            "ls",
            "focus [JOB_TITLE]",
            "rename JOB_TITLE --title NEW_TITLE",
            "delete JOB_TITLE",
            "chat [JOB_TITLE]",
            "steer [--job JOB_TITLE] MESSAGE",
            "pause [JOB_TITLE] [note...]",
            "resume [JOB_TITLE]",
            "cancel [JOB_TITLE] [note...]",
        ),
    )
    _print_group(
        "Inspect",
        (
            "status [JOB_TITLE]",
            "health",
            "history [JOB_TITLE]",
            "events [JOB_TITLE] [--follow] [--json]",
            "activity [JOB_TITLE] [--follow]",
            "updates [JOB_TITLE]",
            "outputs [JOB_TITLE] --verbose",
            "findings [JOB_TITLE]",
            "tasks [JOB_TITLE]",
            "roadmap [JOB_TITLE]",
            "experiments [JOB_TITLE]",
            "sources [JOB_TITLE]",
            "memory [JOB_TITLE]",
            "metrics [JOB_TITLE]",
            "usage [JOB_TITLE]",
            "artifacts [JOB_TITLE]",
            "artifact QUERY_OR_TITLE",
            "lessons [JOB_TITLE]",
        ),
    )
    _print_group(
        "Worker",
        (
            "work [JOB_TITLE] --steps N [--verbose]",
            "run [JOB_TITLE] --poll-seconds N",
            "start --poll-seconds N",
            "restart --poll-seconds N",
            "stop  # daemon",
            "stop [JOB_TITLE]  # pause job",
        ),
    )
    _print_group(
        "System",
        (
            "learn [--job JOB_TITLE] LESSON",
            "digest JOB_TITLE",
            "daily-digest",
            "update",
            "service install|status|uninstall",
            "autostart install|status|uninstall",
            "dashboard [JOB_TITLE] --no-follow",
            "doctor --check-model",
            "browser-dashboard --port 4848",
            "help",
            "exit",
        ),
    )


def _print_group(title: str, commands: tuple[str, ...]) -> None:
    print(title)
    for command in commands:
        print(f"  {command}")
    print()

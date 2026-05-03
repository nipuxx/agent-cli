import json

from nipux_cli.artifacts import ArtifactStore
from nipux_cli import __version__
from nipux_cli.chat_frame_runtime import frame_next_job_id as _frame_next_job_id
from nipux_cli.chat_frame_runtime import frame_refresh_interval as _frame_refresh_interval
from nipux_cli.cli import (
    _build_first_run_frame,
    _build_chat_frame,
    _build_chat_messages,
    _chat_handle_line,
    _chat_control_command,
    _config_field_value,
    _emit_frame_if_changed,
    _first_run_click_action,
    _handle_first_run_menu_line,
    _handle_first_run_frame_line,
    _handle_chat_message,
    _launch_agent_plist,
    _load_frame_snapshot,
    _minimal_live_event_line,
    _print_shell_help,
    _run_shell_line,
    _save_config_field,
    _slash_suggestion_lines,
    _systemd_service_text,
    build_parser,
    main,
)
from nipux_cli.config import load_config
from nipux_cli.daemon import append_daemon_event
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse
from nipux_cli.settings import inline_setting_notice as _inline_setting_notice
from nipux_cli.tui_commands import (
    CHAT_SLASH_COMMANDS,
    FIRST_RUN_SLASH_COMMANDS,
    autocomplete_slash as _autocomplete_slash,
    cycle_slash as _cycle_slash,
)
from nipux_cli.tui_events import chat_pane_lines
from nipux_cli.tui_input import decode_terminal_escape as _decode_terminal_escape
from nipux_cli.tui_outcomes import hourly_update_lines, recent_model_update_lines


def _mode(path):
    return path.stat().st_mode & 0o777


def test_cli_has_operator_commands():
    parser = build_parser()

    assert parser.parse_args(["shell", "--status"]).func.__name__ == "cmd_shell"
    assert parser.parse_args(["status", "--full"]).func.__name__ == "cmd_status"
    assert parser.parse_args(["health"]).func.__name__ == "cmd_health"
    assert parser.parse_args(["history"]).func.__name__ == "cmd_history"
    assert parser.parse_args(["events", "--follow"]).func.__name__ == "cmd_events"
    assert parser.parse_args(["activity", "--follow"]).func.__name__ == "cmd_activity"
    assert parser.parse_args(["feed"]).func.__name__ == "cmd_activity"
    assert parser.parse_args(["updates"]).func.__name__ == "cmd_updates"
    assert parser.parse_args(["update"]).func.__name__ == "cmd_updates"
    assert parser.parse_args(["outcomes"]).func.__name__ == "cmd_updates"
    assert parser.parse_args(["outcomes", "--all"]).all is True
    assert parser.parse_args(["steer", "focus", "sources"]).func.__name__ == "cmd_steer"
    assert parser.parse_args(["say", "focus", "sources"]).func.__name__ == "cmd_steer"
    assert parser.parse_args(["pause"]).func.__name__ == "cmd_pause"
    assert parser.parse_args(["resume"]).func.__name__ == "cmd_resume"
    assert parser.parse_args(["resume", "research", "finder"]).job_id == ["research", "finder"]
    assert parser.parse_args(["cancel"]).func.__name__ == "cmd_cancel"
    assert parser.parse_args(["dashboard", "--no-follow"]).func.__name__ == "cmd_dashboard"
    assert parser.parse_args(["dash", "--no-follow"]).func.__name__ == "cmd_dashboard"
    assert parser.parse_args(["focus", "research"]).func.__name__ == "cmd_focus"
    assert parser.parse_args(["rename", "research", "--title", "new research"]).func.__name__ == "cmd_rename"
    assert parser.parse_args(["delete", "research"]).func.__name__ == "cmd_delete"
    assert parser.parse_args(["rm", "research"]).func.__name__ == "cmd_delete"
    assert parser.parse_args(["chat", "research", "finder"]).func.__name__ == "cmd_chat"
    assert parser.parse_args(["start", "--poll-seconds", "1"]).func.__name__ == "cmd_start"
    assert parser.parse_args(["stop"]).func.__name__ == "cmd_stop"
    assert parser.parse_args(["restart"]).func.__name__ == "cmd_restart"
    assert parser.parse_args(["stop", "research", "finder"]).func.__name__ == "cmd_stop"
    assert parser.parse_args(["stop", "research", "finder"]).job_id == ["research", "finder"]
    assert parser.parse_args(["ls"]).func.__name__ == "cmd_jobs"
    assert parser.parse_args(["autostart", "status"]).func.__name__ == "cmd_autostart"
    assert parser.parse_args(["browser-dashboard", "--port", "4848"]).func.__name__ == "cmd_browser_dashboard"
    assert parser.parse_args(["artifacts"]).func.__name__ == "cmd_artifacts"
    assert parser.parse_args(["artifact", "art_123"]).func.__name__ == "cmd_artifact"
    assert parser.parse_args(["artifact", "Findings", "Batch"]).func.__name__ == "cmd_artifact"
    assert parser.parse_args(["lessons"]).func.__name__ == "cmd_lessons"
    assert parser.parse_args(["learn", "low-evidence", "pages", "are", "bad"]).func.__name__ == "cmd_learn"
    assert parser.parse_args(["findings"]).func.__name__ == "cmd_findings"
    assert parser.parse_args(["tasks"]).func.__name__ == "cmd_tasks"
    assert parser.parse_args(["roadmap"]).func.__name__ == "cmd_roadmap"
    assert parser.parse_args(["experiments"]).func.__name__ == "cmd_experiments"
    assert parser.parse_args(["sources"]).func.__name__ == "cmd_sources"
    assert parser.parse_args(["memory"]).func.__name__ == "cmd_memory"
    assert parser.parse_args(["metrics"]).func.__name__ == "cmd_metrics"
    assert parser.parse_args(["usage"]).func.__name__ == "cmd_usage"
    assert parser.parse_args(["outputs", "research", "finder"]).func.__name__ == "cmd_logs"
    assert parser.parse_args(["outputs"]).func.__name__ == "cmd_logs"
    assert parser.parse_args(["service", "status"]).func.__name__ == "cmd_service"
    assert parser.parse_args(["work", "--steps", "2", "--fake"]).func.__name__ == "cmd_work"
    assert parser.parse_args(["run", "--no-follow"]).func.__name__ == "cmd_run"


def test_cli_version_flag(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    assert f"nipux {__version__}" in capsys.readouterr().out


def test_python_module_entrypoint_uses_cli_main():
    import nipux_cli.__main__ as module_entrypoint

    assert module_entrypoint.main is main


def test_init_openrouter_writes_secret_free_config_and_env_template(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    main(["init", "--openrouter", "--model", "provider/model"])

    out = capsys.readouterr().out
    config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "Wrote" in out
    assert "name: provider/model" in config_text
    assert "base_url: https://openrouter.ai/api/v1" in config_text
    assert "api_key_env: OPENROUTER_API_KEY" in config_text
    assert "sk-" not in config_text
    assert env_text.strip().endswith("OPENROUTER_API_KEY" + "=")
    assert "sk-" not in env_text
    assert _mode(tmp_path / "config.yaml") == 0o600
    assert _mode(tmp_path / ".env") == 0o600


def test_init_defaults_to_qwen_openrouter(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    main(["init"])

    config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "name: qwen/qwen3.6-27b" in config_text
    assert "base_url: https://openrouter.ai/api/v1" in config_text
    assert "api_key_env: OPENROUTER_API_KEY" in config_text
    assert env_text.strip().endswith("OPENROUTER_API_KEY" + "=")


def test_init_openrouter_defaults_to_qwen36(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    main(["init", "--openrouter"])

    config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "name: qwen/qwen3.6-27b" in config_text
    assert "base_url: https://openrouter.ai/api/v1" in config_text
    assert "api_key_env: OPENROUTER_API_KEY" in config_text


def test_shell_freeform_text_adds_operator_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _run_shell_line("focus on real evidence sources, not irrelevant sources") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "waiting for research" in out
        assert (
            job["metadata"]["operator_messages"][-1]["message"]
            == "focus on real evidence sources, not irrelevant sources"
        )
    finally:
        db.close()


def test_main_no_args_enters_chat_first_home(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
        db.append_operator_message(job_id, "remember this visible note", source="test")
        db.append_agent_update(job_id, "visible agent update", category="chat")
    finally:
        db.close()

    def eof_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof_input)

    main([])

    out = capsys.readouterr().out
    assert "WORKSPACE" in out
    assert "RECENT ACTIVITY - research" in out
    assert "remember this visible note" in out
    assert "visible agent update" in out


def test_main_no_args_with_no_jobs_shows_first_run_menu(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    def eof_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof_input)

    main([])

    out = capsys.readouterr().out
    assert "Start" in out
    assert "FIRST RUN" not in out
    assert "new       create a long-running job" in out
    assert "Create or manage jobs" not in out
    assert "settings" not in out.lower()
    assert "shell     open the full command console" not in out
    assert "doctor    check local setup" in out
    assert "init      write config/env template" in out
    assert "_   _" not in out
    assert "nipux menu >" not in out


def test_first_run_menu_can_create_job_and_open_workspace(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    opened = {}
    started = {}

    def fake_input(_prompt):
        return "new Build a durable workflow"

    def fake_enter_chat(job_id, *, show_history, history_limit):
        opened["job_id"] = job_id
        opened["show_history"] = show_history
        opened["history_limit"] = history_limit
        print(f"opened {job_id}")

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("nipux_cli.cli._enter_chat", fake_enter_chat)
    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)

    main([])

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        jobs = db.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Build a durable workflow"
        assert opened["job_id"] == jobs[0]["id"]
        assert opened["show_history"] is True
        assert opened["history_limit"] == 12
    finally:
        db.close()
    assert started["poll_seconds"] == 0.0
    assert started["quiet"] is True
    assert "created Build a durable workflow" in out
    assert "Worker started" in out
    assert "opened" in out


def test_first_run_plain_greeting_does_not_create_job(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    assert _handle_first_run_menu_line("Hello") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        assert db.list_jobs() == []
    finally:
        db.close()
    assert "long-running work" in out


def test_first_run_frame_uses_full_screen_ui_not_banner(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    frame = _build_first_run_frame("", [], width=100, height=24)

    assert "Nipux" in frame
    assert "Nipux Chat" in frame
    assert "Workspace" in frame
    assert "Type a goal" in frame
    assert "New job" in frame
    assert "/ commands" in frame
    assert "controls on the right" not in frame
    assert "Control" not in frame
    assert "_   _" in frame
    assert "FIRST RUN" not in frame
    assert "nipux menu >" not in frame
    assert "/shell" not in frame


def test_first_run_frame_has_slash_command_popup(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    frame = _build_first_run_frame("/", [], width=100, height=26)

    assert "commands" in frame
    assert "/new" in frame
    assert "/jobs" in frame
    assert "/model" in frame
    assert "/settings" not in frame
    assert "/shell" not in frame
    assert "type to filter" in frame


def test_first_run_frame_has_no_settings_page(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    frame = _build_first_run_frame("", [], width=100, height=26, view="settings", selected=1)

    assert "Settings" not in frame
    assert "/base-url URL" not in frame
    assert "/api-key KEY" not in frame
    assert "/timeout SECONDS" not in frame
    assert "Mode" in frame
    assert "Start" in frame
    assert "/shell" not in frame


def test_first_run_frame_uses_command_palette_for_config(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    frame = _build_first_run_frame("/model", [], width=100, height=26)

    assert "/model" in frame
    assert "set model" in frame
    assert "Settings" not in frame


def test_settings_editor_persists_model_config(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    assert _save_config_field("model.name", "demo/model") == "demo/model"
    assert _save_config_field("model.context_length", "4096") == 4096
    assert _save_config_field("runtime.daily_digest_enabled", "false") is False

    assert _config_field_value("model.name") == "demo/model"
    assert _config_field_value("model.context_length") == 4096
    assert _config_field_value("runtime.daily_digest_enabled") is False
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "demo/model" in text
    assert _inline_setting_notice("model.name", "") == "kept model.name"


def test_slash_autocomplete_filters_commands():
    assert _autocomplete_slash("/do", FIRST_RUN_SLASH_COMMANDS) == "/doctor "
    assert _autocomplete_slash("/mo", FIRST_RUN_SLASH_COMMANDS) == "/model "
    assert _autocomplete_slash("/sta", CHAT_SLASH_COMMANDS) == "/status "
    assert _autocomplete_slash("/rest", CHAT_SLASH_COMMANDS) == "/restart "
    assert _autocomplete_slash("/step", CHAT_SLASH_COMMANDS) == "/step-limit "
    assert _autocomplete_slash("/out", FIRST_RUN_SLASH_COMMANDS) == "/output-chars "
    assert _cycle_slash("/", CHAT_SLASH_COMMANDS, direction=1) == "/run "
    assert _cycle_slash("/", CHAT_SLASH_COMMANDS, direction=-1) == "/exit "
    assert _cycle_slash("/work ", CHAT_SLASH_COMMANDS, direction=1) == "/work "
    assert _cycle_slash("/out", CHAT_SLASH_COMMANDS, direction=1) == "/outcomes "
    assert _cycle_slash("/out", CHAT_SLASH_COMMANDS, direction=-1) == "/output-cost "
    assert _autocomplete_slash("plain text", CHAT_SLASH_COMMANDS) == "plain text"
    lines = _slash_suggestion_lines("/art", CHAT_SLASH_COMMANDS, width=80)
    text = "\n".join(lines)
    assert "/artifacts" in text
    assert "/artifact" in text
    assert "/run" not in text
    hint_text = "\n".join(_slash_suggestion_lines("/model ", CHAT_SLASH_COMMANDS, width=80))
    assert "/model" in hint_text
    assert "MODEL" in hint_text
    partial_hint_text = "\n".join(_slash_suggestion_lines("/mo", CHAT_SLASH_COMMANDS, width=80))
    assert "/model MODEL" in partial_hint_text
    assert "↑↓ select" in partial_hint_text
    full_palette_text = "\n".join(_slash_suggestion_lines("/", CHAT_SLASH_COMMANDS, width=80, limit=5))
    assert "type to filter" in full_palette_text
    assert "/shell" not in "\n".join(_slash_suggestion_lines("/", CHAT_SLASH_COMMANDS, width=80, limit=20))
    assert "/restart" in "\n".join(_slash_suggestion_lines("/re", CHAT_SLASH_COMMANDS, width=80, limit=20))


def test_terminal_escape_decodes_arrows_and_mouse_click():
    assert _decode_terminal_escape("\x1b[A") == ("up", None)
    assert _decode_terminal_escape("\x1b[B") == ("down", None)
    assert _decode_terminal_escape("\x1b[C") == ("right", None)
    assert _decode_terminal_escape("\x1b[D") == ("left", None)
    assert _decode_terminal_escape("\x1bOB") == ("down", None)
    assert _decode_terminal_escape("\x1b[1;2B") == ("down", None)
    assert _decode_terminal_escape("\x1b[<0;88;12M") == ("click", (88, 12))
    assert _decode_terminal_escape("\x1b[M !!") == ("click", (1, 1))


def test_first_run_click_maps_right_pane_actions(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=(100, 30): (100, 30))

    assert _first_run_click_action(70, 11, view="start") == 0
    assert _first_run_click_action(70, 13, view="start") == 2
    assert _first_run_click_action(10, 12, view="start") is None


def test_frame_next_job_cycles_jobs():
    snapshot = {"jobs": [{"id": "one"}, {"id": "two"}, {"id": "three"}]}

    assert _frame_next_job_id(snapshot, "one", direction=1) == "two"
    assert _frame_next_job_id(snapshot, "one", direction=-1) == "three"
    assert _frame_next_job_id(snapshot, "missing", direction=1) == "two"


def test_frame_refresh_slows_background_updates_while_typing():
    assert _frame_refresh_interval("") < _frame_refresh_interval("drafting a message")


def test_chat_help_has_config_slash_commands_without_settings_page(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/help") is True

    out = capsys.readouterr().out
    assert "/settings" not in out
    assert "/usage" in out
    assert "/config" in out
    assert "/outcomes" in out
    assert "/model MODEL" in out
    assert "/api-key KEY" in out
    assert "/timeout SECONDS" in out
    assert "/home PATH" in out
    assert "/digest-time HH:MM" in out
    assert "/shell" not in out


def test_chat_slash_palette_matches_public_chat_commands():
    palette = {command for command, _description in CHAT_SLASH_COMMANDS}
    advertised = {
        "/jobs",
        "/focus",
        "/switch",
        "/new",
        "/delete",
        "/history",
        "/events",
        "/activity",
        "/outputs",
        "/updates",
        "/outcomes",
        "/status",
        "/usage",
        "/config",
        "/health",
        "/artifacts",
        "/artifact",
        "/findings",
        "/tasks",
        "/roadmap",
        "/experiments",
        "/sources",
        "/memory",
        "/metrics",
        "/lessons",
        "/model",
        "/base-url",
        "/api-key",
        "/api-key-env",
        "/context",
        "/input-cost",
        "/output-cost",
        "/timeout",
        "/home",
        "/step-limit",
        "/output-chars",
        "/daily-digest",
        "/digest-time",
        "/doctor",
        "/run",
        "/start",
        "/restart",
        "/work",
        "/work-verbose",
        "/stop",
        "/pause",
        "/resume",
        "/cancel",
        "/learn",
        "/note",
        "/follow",
        "/digest",
        "/clear",
        "/exit",
    }

    assert advertised <= palette
    assert "/settings" not in palette
    assert "/shell" not in palette


def test_chat_settings_slash_commands_persist_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    monkeypatch.setenv("NIPUX_TEST_KEY", "")
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/model provider/model") is True
    assert _chat_handle_line(job_id, "/base-url https://example.com/v1") is True
    assert _chat_handle_line(job_id, "/context 8192") is True
    assert _chat_handle_line(job_id, "/input-cost 0.10") is True
    assert _chat_handle_line(job_id, "/output-cost 0.20") is True
    assert _chat_handle_line(job_id, "/timeout 45") is True
    assert _chat_handle_line(job_id, "/step-limit 90") is True
    assert _chat_handle_line(job_id, "/output-chars 4096") is True
    assert _chat_handle_line(job_id, "/daily-digest false") is True
    assert _chat_handle_line(job_id, "/digest-time 08:30") is True
    assert _chat_handle_line(job_id, "/api-key-env NIPUX_TEST_KEY") is True
    assert _chat_handle_line(job_id, "/api-key sk-test-value") is True
    out = capsys.readouterr().out

    assert "saved model.name = provider/model" in out
    assert "saved model.base_url = https://example.com/v1" in out
    assert "saved model.context_length = 8192" in out
    assert "saved model.input_cost_per_million = 0.1" in out
    assert "saved model.output_cost_per_million = 0.2" in out
    assert "saved model.request_timeout_seconds = 45.0" in out
    assert "saved runtime.max_step_seconds = 90" in out
    assert "saved runtime.artifact_inline_char_limit = 4096" in out
    assert "saved runtime.daily_digest_enabled = False" in out
    assert "saved runtime.daily_digest_time = 08:30" in out
    assert "saved model.api_key_env = NIPUX_TEST_KEY" in out
    assert "saved NIPUX_TEST_KEY" in out
    assert "sk-test-value" not in out
    assert _mode(tmp_path / "config.yaml") == 0o600
    assert _mode(tmp_path / ".env") == 0o600
    assert _config_field_value("model.name") == "provider/model"
    assert _config_field_value("model.base_url") == "https://example.com/v1"
    assert _config_field_value("model.context_length") == 8192
    assert _config_field_value("model.input_cost_per_million") == 0.1
    assert _config_field_value("model.output_cost_per_million") == 0.2
    assert _config_field_value("model.request_timeout_seconds") == 45.0
    assert _config_field_value("runtime.max_step_seconds") == 90
    assert _config_field_value("runtime.artifact_inline_char_limit") == 4096
    assert _config_field_value("runtime.daily_digest_enabled") is False
    assert _config_field_value("runtime.daily_digest_time") == "08:30"
    assert "NIPUX_TEST_KEY=sk-test-value" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_chat_config_slash_command_summarizes_runtime_without_secret(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    monkeypatch.setenv("NIPUX_TEST_KEY", "sk-test-value")
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()
    (tmp_path / "config.yaml").write_text(
        """
model:
  name: provider/model
  base_url: https://example.com/v1
  api_key_env: NIPUX_TEST_KEY
  context_length: 8192
  request_timeout_seconds: 45
  input_cost_per_million: 0.1
  output_cost_per_million: 0.2
runtime:
  max_step_seconds: 90
  artifact_inline_char_limit: 4096
  daily_digest_enabled: false
  daily_digest_time: "08:30"
""",
        encoding="utf-8",
    )

    assert _chat_handle_line(job_id, "/config") is True

    out = capsys.readouterr().out
    assert "config" in out
    assert "model: provider/model" in out
    assert "endpoint: https://example.com/v1" in out
    assert "key: set (NIPUX_TEST_KEY)" in out
    assert "context: 8192" in out
    assert "cost rates: input $0.1 / output $0.2 per 1M tokens" in out
    assert "sk-test-value" not in out


def test_chat_usage_slash_command_reports_tokens(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
        db.append_event(
            job_id,
            event_type="loop",
            title="message_end",
            metadata={
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 250,
                    "total_tokens": 1250,
                    "cost": 0.0042,
                }
            },
        )
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/usage") is True

    out = capsys.readouterr().out
    assert "usage research" in out
    assert "tokens: total=1.2K prompt=1.0K output=250" in out
    assert "cost=$0.0042" in out


def test_chat_usage_estimates_cost_from_configured_rates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  name: provider/model
  base_url: https://example.com/v1
  input_cost_per_million: 1.0
  output_cost_per_million: 2.0
""",
        encoding="utf-8",
    )
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
        db.append_event(
            job_id,
            event_type="loop",
            title="message_end",
            metadata={
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "estimated": True,
                }
            },
        )
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/usage") is True

    out = capsys.readouterr().out
    assert "cost=~$0.0020" in out


def test_first_run_settings_slash_commands_persist_config(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    action, payload = _handle_first_run_frame_line("/model provider/model")

    assert action == "notice"
    assert isinstance(payload, list)
    assert any("saved model.name = provider/model" in line for line in payload)
    assert _config_field_value("model.name") == "provider/model"


def test_shell_ls_alias_lists_jobs_instead_of_steering(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _run_shell_line("ls") is True

    out = capsys.readouterr().out
    assert "research" in out
    assert "queued for" not in out


def test_roadmap_command_renders_roadmap(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Broad work", title="broad")
        db.append_roadmap_record(
            job_id,
            title="Broad Roadmap",
            status="active",
            current_milestone="Foundation",
            milestones=[{
                "title": "Foundation",
                "status": "validating",
                "validation_status": "pending",
                "features": [{"title": "First feature", "status": "done"}],
            }],
        )
    finally:
        db.close()

    main(["roadmap", "broad"])

    out = capsys.readouterr().out
    assert "roadmap broad" in out
    assert "Broad Roadmap" in out
    assert "Foundation" in out
    assert "validation=pending" in out


def test_shell_focus_controls_default_steering_job(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        first = db.create_job("Research topic", title="first research")
        second = db.create_job("Find investors", title="investor search")
    finally:
        db.close()

    assert _run_shell_line("focus investor") is True
    assert _run_shell_line("prioritize Toronto findings") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        first_job = db.get_job(first)
        second_job = db.get_job(second)
        assert "focus set:" in out
        assert first_job["metadata"].get("operator_messages") is None
        assert second_job["metadata"]["operator_messages"][-1]["message"] == "prioritize Toronto findings"
    finally:
        db.close()


def test_shell_rename_updates_job_title_and_program(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="old title")
        program = tmp_path / "jobs" / job_id / "program.md"
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text("# old title\n\nBody\n", encoding="utf-8")
    finally:
        db.close()

    assert _run_shell_line("rename old title --title new title") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "renamed old title -> new title" in out
        assert job["title"] == "new title"
        assert program.read_text(encoding="utf-8").startswith("# new title\n")
    finally:
        db.close()


def test_shell_delete_removes_job_and_artifact_dir(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="delete me")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        store = ArtifactStore(tmp_path, db=db)
        stored = store.write_text(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            title="Artifact",
            summary="saved",
            content="content",
        )
        artifact_path = stored.path
    finally:
        db.close()

    assert artifact_path.exists()
    assert _run_shell_line("delete delete me") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        assert "deleted delete me" in out
        try:
            db.get_job(job_id)
        except KeyError:
            pass
        else:
            raise AssertionError("job still exists after shell delete")
        assert not artifact_path.exists()
        assert not (tmp_path / "jobs" / job_id).exists()
    finally:
        db.close()


def test_shell_help_has_no_examples_or_control_run_sections(capsys):
    _print_shell_help()

    out = capsys.readouterr().out
    assert "Examples:" not in out
    assert "\nControl\n" not in out
    assert "\nRun\n" not in out
    assert "delete JOB_TITLE" in out
    assert "usage [JOB_TITLE]" in out
    assert "Jobs" in out
    assert "Worker" in out


def test_chat_clear_does_not_queue_operator_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _chat_handle_line(job_id, "clear") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "\033[2J\033[H" in out
        assert job["metadata"].get("operator_messages") is None
    finally:
        db.close()


def test_minimal_live_event_line_summarizes_tool_steps():
    line = _minimal_live_event_line(
        {
            "event_type": "tool_call",
            "title": "shell_exec",
            "body": "",
            "metadata": {"input": {"arguments": {"command": "ssh server nvidia-smi"}}},
        }
    )

    assert line == "start shell ssh server nvidia-smi"


def test_chat_frame_is_bounded_and_has_composer():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "keep a generic long-running job visible",
            "status": "running",
            "kind": "generic",
            "metadata": {"task_queue": [{"status": "active", "title": "Draft next deliverable", "priority": 7}]},
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [
            {
                "step_no": 3,
                "status": "completed",
                "kind": "tool",
                "tool_name": "web_search",
                "summary": "web_search returned sources",
            }
        ],
        "artifacts": [{"id": "art_demo"}],
        "memory_entries": [{}],
        "events": [
            {
                "event_type": "agent_message",
                "title": "plan",
                "body": "I will plan this.\nPlan:\n- one\n- two\nQuestions:\n- answer?",
                "metadata": {},
            },
            {
                "event_type": "task",
                "title": "internal task",
                "body": "internal task body",
                "metadata": {},
            },
            {
                "event_type": "tool_result",
                "title": "web_search",
                "body": "web_search query='demo' returned 1 results",
                "metadata": {"status": "completed", "input": {"arguments": {"query": "demo"}}},
            }
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
        "base_url": "https://openrouter.ai/api/v1",
        "context_length": 8192,
        "token_usage": {
            "calls": 2,
            "latest_prompt_tokens": 4096,
            "completion_tokens": 1234,
            "total_tokens": 5330,
            "cost": 0.0123,
            "has_cost": True,
        },
    }

    frame = _build_chat_frame(snapshot, "hello", [], width=100, height=22)
    wide_frame = _build_chat_frame(snapshot, "", [], width=140, height=22)

    assert len(frame.splitlines()) <= 22
    assert "Nipux CLI" in frame
    assert "Chat" in frame
    assert "Status" in frame
    assert "Outcome" in frame
    assert "#3" not in frame
    assert "Jobs" in frame
    assert "Recent outcomes" in frame
    assert "ctx" in frame
    assert "4.1K/8.2K" in frame
    assert "out" in frame
    assert "1.2K" in frame
    assert "tok" in frame
    assert "5.3K" in frame
    assert "$0.0123" in frame
    assert "daemon running  model model/demo  ctx 4.1K/8.2K" in wide_frame
    assert wide_frame.splitlines()[1].startswith("─")
    assert "Enter send" in frame
    assert "❯ hello" in frame
    task_frame = _build_chat_frame(snapshot, "", [], width=100, height=26)
    assert "Draft next deli" in task_frame

    work = _build_chat_frame(snapshot, "", [], width=100, height=24, right_view="work")
    assert "Work" in work
    assert "Tool / console" in work
    assert "search demo" in work

    updates = _build_chat_frame(snapshot, "", [], width=100, height=24, right_view="updates")
    assert "Outcomes" in updates
    assert "Outcomes by hour" in updates

    secret = _build_chat_frame(
        snapshot,
        "secret-value",
        [],
        width=100,
        height=24,
        editing_field="secret:model.api_key",
    )
    assert "Editing API key" in secret
    assert "secret-value" not in secret
    assert "••••" in secret


def test_chat_frame_separates_chat_from_worker_activity():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "keep chat separate",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [
            {"event_type": "operator_message", "body": "start a benchmark job", "metadata": {}},
            {"event_type": "agent_message", "title": "chat", "body": "I created the job and started it.", "metadata": {}},
            {"event_type": "tool_call", "title": "shell_exec", "body": "", "metadata": {"input": {"arguments": {"command": "python bench.py"}}}},
            {"event_type": "tool_result", "title": "shell_exec", "body": "shell_exec rc=0", "metadata": {"status": "completed", "input": {"arguments": {"command": "python bench.py"}}}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=130, height=24, right_view="work")
    chat_side = frame.split(" │ ", 1)[0]

    assert "start a benchmark job" in frame
    assert "I created the job" in frame
    assert "Tool / console" in frame
    assert "python bench.py" in frame
    assert "python bench.py" not in chat_side


def test_chat_frame_empty_state_uses_sleek_hero():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "keep chat visible",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=120, height=28)

    assert "_   _ ___" in frame
    assert "Talk to create, steer, inspect" in frame
    assert "No chat yet." not in frame


def test_frame_emit_skips_unchanged_render(capsys):
    first = _emit_frame_if_changed("line one\nline two")
    second = _emit_frame_if_changed("frame", first)
    third = _emit_frame_if_changed("frame\nline three", second)

    out = capsys.readouterr().out
    assert first == "line one\nline two"
    assert second == "frame"
    assert third == "frame\nline three"
    assert out.count("\033[H") == 1
    assert "\033[1;1Hframe" in out
    assert "\033[2;1H        " in out
    assert "\033[2K" not in out
    assert "\033[J" not in out


def test_chat_frame_does_not_cap_long_agent_messages():
    long_reply = (
        "**Completed Work:** "
        "1. Test suite analysis finished. "
        "2. Code analysis findings documented. "
        "3. Market readiness gaps identified. "
        "4. Packaging risks summarized. "
        "5. Daemon reliability checked. "
        "6. UI ergonomics reviewed. "
        "7. Final recommendation included."
    )
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "keep chat readable",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [
            {"event_type": "operator_message", "body": "what have you done so far", "metadata": {}},
            {"event_type": "agent_message", "title": "chat", "body": long_reply, "metadata": {}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=118, height=32)

    assert "Completed Work:" in frame
    assert "Final recommendation included" in frame
    assert "…" not in frame


def test_plain_chat_control_intents_map_to_commands():
    assert _chat_control_command("how is it going?") == "/status"
    assert _chat_control_command("what is blocking it?") == "/status"
    assert _chat_control_command("start working") == "/run"
    assert _chat_control_command("pause this job") == "/pause"
    assert _chat_control_command("show jobs") == "/jobs"
    assert _chat_control_command("change model") == "/model"
    assert _chat_control_command("how much did it cost") == "/usage"
    assert _chat_control_command("what has it done") == "/outcomes"
    assert _chat_control_command("what have you done so far") == "/outcomes"
    assert _chat_control_command("what did the model do") == "/outcomes"
    assert _chat_control_command("what have all jobs done") == "/outcomes all"
    assert _chat_control_command("what files did it create") == "/artifacts"
    assert _chat_control_command("show me the saved files") == "/artifacts"
    assert _chat_control_command("what tool calls did it run") == "/activity"
    assert _chat_control_command("show console output") == "/outputs"
    assert _chat_control_command("what tasks are open") == "/tasks"
    assert _chat_control_command("show the current plan") == "/roadmap"
    assert _chat_control_command("show benchmarks") == "/experiments"
    assert _chat_control_command("how many tokens did it use") == "/usage"
    assert _chat_control_command("restart daemon") == "/restart"
    assert _chat_control_command("prefer artifact-backed findings") == ""


def test_plain_chat_control_intent_does_not_queue_operator_context(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()

    captured = {}

    def fake_capture(job_id_arg, command):
        captured["job_id"] = job_id_arg
        captured["command"] = command
        return True, "status output\n"

    monkeypatch.setattr("nipux_cli.cli._capture_chat_command", fake_capture)

    keep_running, message = _handle_chat_message(job_id, "how is it going?", quiet=True)

    assert keep_running is True
    assert message == "status output"
    assert captured == {"job_id": job_id, "command": "/status"}
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert job["metadata"].get("operator_messages") is None
    finally:
        db.close()


def test_plain_chat_reply_usage_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
    finally:
        db.close()

    keep_running, message = _handle_chat_message(
        job_id,
        "hello",
        quiet=True,
        reply_fn=lambda _job_id, _line: LLMResponse(
            content="reply",
            usage={"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140, "cost": 0.001},
            model="provider/model",
            response_id="gen_chat",
        ),
    )

    db = AgentDB(tmp_path / "state.db")
    try:
        usage = db.job_token_usage(job_id)
        events = db.list_events(job_id=job_id, event_types=["loop"], limit=5)
    finally:
        db.close()
    assert keep_running is True
    assert message == ""
    assert usage["calls"] == 1
    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 20
    assert usage["cost"] == 0.001
    assert events[-1]["metadata"]["source"] == "chat"
    assert events[-1]["metadata"]["response_id"] == "gen_chat"


def test_chat_frame_surfaces_actual_work_events():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "produce visible work",
            "status": "running",
            "kind": "generic",
            "metadata": {
                "task_queue": [{"status": "open"}],
                "roadmap": {"milestones": [{"title": "Draft", "status": "active"}]},
            },
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [{"id": "art_demo"}],
        "memory_entries": [{}],
        "events": [
            {"event_type": "operator_message", "body": "please keep improving", "metadata": {"mode": "steer"}},
            {"event_type": "tool_call", "title": "web_search", "body": "", "metadata": {"input": {"arguments": {"query": "agent harness distillation"}}}},
            {"event_type": "tool_result", "title": "web_search", "body": "web_search query='agent harness distillation' returned 5 results", "metadata": {"status": "completed", "input": {"arguments": {"query": "agent harness distillation"}}}},
            {"event_type": "artifact", "title": "Research Paper Draft", "body": "", "metadata": {"summary": "saved first complete draft"}},
            {"event_type": "finding", "title": "Distillation finding", "body": "tool traces improve student behavior", "metadata": {}},
            {"event_type": "task", "title": "Compare methods", "body": "", "metadata": {"status": "open"}},
            {"event_type": "roadmap", "title": "Paper roadmap", "body": "", "metadata": {"status": "active"}},
            {"event_type": "milestone_validation", "title": "Draft", "body": "", "metadata": {"validation_status": "passed"}},
            {"event_type": "experiment", "title": "Citation coverage check", "body": "", "metadata": {"metric_name": "sources", "metric_value": 18, "metric_unit": ""}},
            {"event_type": "lesson", "title": "strategy", "body": "prefer measured updates", "metadata": {}},
            {"event_type": "reflection", "title": "reflection", "body": "Reflection through step #10: next branch is evaluation.", "metadata": {}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
        "counts": {"steps": 10, "artifacts": 1, "memory": 1},
    }

    updates = _build_chat_frame(snapshot, "", [], width=150, height=34, right_view="updates")
    work = _build_chat_frame(snapshot, "", [], width=150, height=34, right_view="work")
    frame = updates + "\n" + work

    assert "Done" in work
    assert "Research Paper Draft" in frame
    assert "Distillation finding" in frame
    assert "Compare methods" in frame
    assert "Paper roadmap" in frame
    assert "passed Draft" in frame
    assert "Citation coverage check" in frame
    assert "LEARN" in frame
    assert "strategy" in frame
    assert "reflected #10" in frame


def test_chat_frame_has_model_updates_page():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "paper job",
            "objective": "write a paper",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "paper job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [
            {"event_type": "tool_result", "title": "web_search", "body": "web_search query='distillation agents' returned 5 results", "metadata": {"status": "completed", "input": {"arguments": {"query": "distillation agents"}}}},
            {"event_type": "artifact", "title": "Literature Review Draft", "body": "saved draft", "metadata": {}},
            {"event_type": "finding", "title": "Trajectory distillation", "body": "teacher traces improve tool use", "metadata": {}},
            {"event_type": "experiment", "title": "Citation density check", "body": "", "metadata": {"metric_name": "citations", "metric_value": 12, "metric_unit": "count"}},
            {"event_type": "tool_result", "title": "write_file", "body": "write_file overwrite /tmp/paper.md", "metadata": {"status": "completed", "input": {"arguments": {"path": "/tmp/paper.md"}}, "output": {"path": "/tmp/paper.md"}}},
            {"event_type": "tool_result", "title": "shell_exec", "body": "shell_exec rc=0", "metadata": {"status": "completed", "input": {"arguments": {"command": "printf draft | tee /tmp/outline.md"}}}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=28, right_view="updates")

    assert "Outcomes" in frame
    assert "Outcomes by hour" in frame
    assert "1 outputs" in frame
    assert "1 measurements" in frame
    assert "Literature Review Draft" in frame
    assert "Trajectory distillation" in frame
    assert "Citation density check" in frame
    assert "paper.md" in frame
    assert "outline.md" in frame


def test_status_job_cards_show_durable_work_mix():
    events = [
        {"event_type": "artifact", "title": "Paper draft", "body": "", "metadata": {}},
        {"event_type": "finding", "title": "Method taxonomy", "body": "", "metadata": {}},
        {
            "event_type": "experiment",
            "title": "Citation coverage check",
            "body": "",
            "metadata": {"metric_name": "citations", "metric_value": 12, "metric_unit": "count"},
        },
    ]
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "paper job",
            "objective": "write a paper",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "paper job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [{"id": "art_1", "title": "Paper draft"}],
        "job_artifacts": {"job_demo": [{"id": "art_1", "title": "Paper draft"}]},
        "job_summary_events": {"job_demo": events},
        "job_counts": {"job_demo": {"artifacts": 1}},
        "memory_entries": [],
        "events": events,
        "summary_events": events,
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=150, height=34)

    assert "work 1 outputs 1 findings 1 measurements" in frame
    assert "made 1 output" in frame
    assert "Paper draft" in frame


def test_recent_outcome_lines_wrap_long_updates():
    lines = recent_model_update_lines(
        [
            {
                "event_type": "finding",
                "title": "Trajectory distillation improves agentic tool selection when teacher traces include failures and recovery actions",
                "body": "",
                "metadata": {},
                "created_at": "2026-05-01T12:34:00+00:00",
            }
        ],
        width=62,
        limit=4,
    )

    rendered = "\n".join(lines)
    assert len(lines) >= 2
    assert "Trajectory distillation improves" in rendered
    assert "teacher traces include" in rendered
    assert "failures" in rendered


def test_recent_outcome_lines_do_not_pretruncate_actual_work():
    events = [
        {
            "event_type": "artifact",
            "title": (
                "Research paper draft rewritten with a new methods section, expanded evaluation table, "
                "and integrated citations from teacher trajectory distillation, agent workflow distillation, "
                "and self-improvement harness papers"
            ),
            "body": "",
            "metadata": {},
            "created_at": "2026-05-01T12:34:00+00:00",
        }
    ]

    rendered = "\n".join(recent_model_update_lines(events, width=72, limit=6))

    assert "methods" in rendered
    assert "section" in rendered
    assert "integrated" in rendered
    assert "citations" in rendered
    assert "self-improvement harness" in rendered
    assert "papers" in rendered
    assert "..." not in rendered


def test_chat_pane_marks_hidden_overflow():
    events = [
        {
            "event_type": "agent_message",
            "title": "chat",
            "body": " ".join(f"word{i}" for i in range(80)),
            "metadata": {},
            "created_at": "2026-04-25T12:00:00Z",
        }
    ]

    lines = chat_pane_lines(events, [], width=48, rows=4)

    assert "word0 word1" in lines[0]
    assert "middle lines hidden" in "\n".join(lines)
    assert "word" in lines[-1]
    assert len(lines) == 4


def test_chat_updates_page_uses_deeper_summary_events():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "paper job",
            "objective": "write a paper",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "paper job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [
            {"event_type": "tool_call", "title": "web_search", "body": "", "metadata": {}},
        ],
        "summary_events": [
            {"event_type": "artifact", "title": "Full Paper Draft", "body": "saved draft", "metadata": {}},
            {"event_type": "finding", "title": "Distillation method map", "body": "", "metadata": {}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=26, right_view="updates")

    assert "Full Paper Draft" in frame
    assert "Distillation method map" in frame


def test_hourly_outcomes_prioritize_durable_work_over_research_noise():
    events = [
        {
            "event_type": "tool_result",
            "title": "web_search",
            "body": "web_search query='generic harness patterns' returned 5 results",
            "metadata": {"status": "completed", "input": {"arguments": {"query": "generic harness patterns"}}},
            "created_at": "2026-05-01T12:05:00+00:00",
        },
        {
            "event_type": "tool_result",
            "title": "web_extract",
            "body": "web_extract fetched 3/3 pages",
            "metadata": {"status": "completed"},
            "created_at": "2026-05-01T12:08:00+00:00",
        },
        {
            "event_type": "artifact",
            "title": "Harness Architecture Notes",
            "body": "saved design notes",
            "metadata": {},
            "created_at": "2026-05-01T12:20:00+00:00",
        },
        {
            "event_type": "experiment",
            "title": "Context budget check",
            "body": "",
            "metadata": {"metric_name": "prompt_tokens", "metric_value": 4200, "metric_unit": "tokens"},
            "created_at": "2026-05-01T12:30:00+00:00",
        },
    ]

    rendered = "\n".join(hourly_update_lines(events, width=96, limit=8))

    assert "2 research" in rendered
    assert "1 outputs" in rendered
    assert "1 measurements" in rendered
    assert "Harness Architecture Notes" in rendered
    assert "Context budget check" in rendered
    assert "generic harness patterns" not in rendered


def test_status_recent_outcomes_hide_research_noise():
    events = [
        {
            "event_type": "tool_result",
            "title": "web_search",
            "body": "web_search query='generic harness patterns' returned 5 results",
            "metadata": {"status": "completed", "input": {"arguments": {"query": "generic harness patterns"}}},
            "created_at": "2026-05-01T12:05:00+00:00",
        },
        {
            "event_type": "artifact",
            "title": "Harness Architecture Notes",
            "body": "saved design notes",
            "metadata": {},
            "created_at": "2026-05-01T12:20:00+00:00",
        },
    ]

    rendered = "\n".join(recent_model_update_lines(events, width=96, limit=4))

    assert "Harness Architecture Notes" in rendered
    assert "generic harness patterns" not in rendered


def test_status_recent_outcomes_hide_plan_update_noise():
    events = [
        {
            "event_type": "reflection",
            "title": "reflection",
            "body": "summarized current counts",
            "metadata": {},
            "created_at": "2026-05-01T12:05:00+00:00",
        },
        {
            "event_type": "agent_message",
            "title": "progress",
            "body": "Checkpoint at step #100.",
            "metadata": {},
            "created_at": "2026-05-01T12:08:00+00:00",
        },
        {
            "event_type": "finding",
            "title": "Teacher trace distillation pattern",
            "body": "",
            "metadata": {},
            "created_at": "2026-05-01T12:20:00+00:00",
        },
    ]

    rendered = "\n".join(recent_model_update_lines(events, width=96, limit=5))

    assert "Teacher trace distillation pattern" in rendered
    assert "Checkpoint at step" not in rendered
    assert "summarized current counts" not in rendered


def test_status_recent_outcomes_show_durable_checkpoint_updates():
    events = [
        {
            "event_type": "agent_message",
            "title": "progress",
            "body": "Checkpoint step #90: ~1 task updated, 1 task resolved.",
            "metadata": {
                "updates": {"tasks": 1},
                "resolutions": {"tasks": 1},
                "deltas": {"findings": 0},
            },
            "created_at": "2026-05-01T12:08:00+00:00",
        }
    ]

    rendered = "\n".join(recent_model_update_lines(events, width=96, limit=4))

    assert "TASK" in rendered
    assert "~1 task updated" in rendered
    assert "1 task resolved" in rendered
    assert "Checkpoint step #90" in rendered


def test_status_recent_outcomes_compact_repeated_updates():
    events = [
        {
            "event_type": "agent_message",
            "title": "error",
            "body": "Model provider requires operator action.",
            "metadata": {},
            "created_at": f"2026-05-01T12:0{index}:00+00:00",
        }
        for index in range(3)
    ]

    rendered = "\n".join(recent_model_update_lines(events, width=96, limit=4))

    assert rendered.count("Model provider requires operator action") == 1
    assert "x3" in rendered


def test_hourly_outcomes_hide_plan_update_noise():
    events = [
        {
            "event_type": "reflection",
            "title": "reflection",
            "body": "summarized current counts",
            "metadata": {},
            "created_at": "2026-05-01T12:05:00+00:00",
        },
        {
            "event_type": "agent_message",
            "title": "progress",
            "body": "Checkpoint at step #100.",
            "metadata": {},
            "created_at": "2026-05-01T12:08:00+00:00",
        },
        {
            "event_type": "artifact",
            "title": "Saved research draft",
            "body": "",
            "metadata": {},
            "created_at": "2026-05-01T12:20:00+00:00",
        },
    ]

    rendered = "\n".join(hourly_update_lines(events, width=96, limit=6))

    assert "Saved research draft" in rendered
    assert "Checkpoint at step" not in rendered
    assert "summarized current counts" not in rendered


def test_hourly_outcomes_count_durable_checkpoint_updates():
    events = [
        {
            "event_type": "agent_message",
            "title": "progress",
            "body": "Checkpoint step #110: ~1 experiment updated, 1 experiment resolved.",
            "metadata": {
                "updates": {"experiments": 1},
                "resolutions": {"experiments": 1},
            },
            "created_at": "2026-05-01T12:08:00+00:00",
        }
    ]

    rendered = "\n".join(hourly_update_lines(events, width=96, limit=6))

    assert "1 measurements" in rendered
    assert "~1 measurement updated" in rendered
    assert "1 measurement resolved" in rendered


def test_hourly_outcome_summary_uses_progress_order():
    events = [
        {
            "event_type": "source",
            "title": "source scored",
            "body": "",
            "metadata": {},
            "created_at": "2026-05-01T12:01:00+00:00",
        },
        {
            "event_type": "artifact",
            "title": "draft saved",
            "body": "",
            "metadata": {},
            "created_at": "2026-05-01T12:02:00+00:00",
        },
        {
            "event_type": "experiment",
            "title": "metric checked",
            "body": "",
            "metadata": {"metric_name": "score", "metric_value": 1, "metric_unit": "point"},
            "created_at": "2026-05-01T12:03:00+00:00",
        },
    ]

    rendered = "\n".join(hourly_update_lines(events, width=96, limit=8))

    assert "1 outputs 1 measurements 1 sources" in rendered


def test_hourly_outcomes_wrap_long_durable_updates_without_pre_truncation():
    events = [
        {
            "event_type": "finding",
            "title": (
                "Distillation survey breakthrough: teacher trajectories should include failed tool calls, "
                "operator corrections, recovery steps, and measured validation so the student learns the "
                "whole harness loop instead of only final answers"
            ),
            "body": "",
            "metadata": {},
            "created_at": "2026-05-01T12:05:00+00:00",
        },
    ]

    rendered = "\n".join(hourly_update_lines(events, width=82, limit=6))

    assert "operator corrections" in rendered
    assert "measured" in rendered
    assert "validation" in rendered
    assert "only" in rendered
    assert "final answers" in rendered
    assert "..." not in rendered


def test_hourly_outcomes_limit_visible_hours_without_losing_headers():
    events = []
    for hour in range(8):
        events.extend(
            [
                {
                    "event_type": "artifact",
                    "title": f"Draft saved hour {hour}",
                    "body": "",
                    "metadata": {},
                    "created_at": f"2026-05-01T{hour:02d}:05:00+00:00",
                },
                {
                    "event_type": "finding",
                    "title": f"Finding hour {hour}",
                    "body": "",
                    "metadata": {},
                    "created_at": f"2026-05-01T{hour:02d}:20:00+00:00",
                },
            ]
        )

    rendered = "\n".join(hourly_update_lines(events, width=96, limit=8))

    assert "2026-05-01 06:00" in rendered
    assert "2026-05-01 07:00" in rendered
    assert "Draft saved hour 7" in rendered
    assert "Finding hour 7" in rendered
    assert "Draft saved hour 0" not in rendered


def test_chat_updates_page_includes_agent_error_updates():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "provider job",
            "objective": "keep provider state visible",
            "status": "paused",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "provider job", "status": "paused", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [],
        "summary_events": [
            {
                "event_type": "agent_message",
                "title": "error",
                "body": "Model provider requires operator action.",
                "metadata": {"reason": "llm_provider_blocked"},
            },
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    updates = _build_chat_frame(snapshot, "", [], width=132, height=34, right_view="updates")
    status = _build_chat_frame(snapshot, "", [], width=132, height=34)

    assert "Model provider requires" in updates
    assert "operator action" in updates
    assert "Outcome" in status
    assert "Model provider re" in status


def test_chat_status_marks_provider_blocked_jobs_before_daemon_retry():
    job = {
        "id": "job_demo",
        "title": "provider job",
        "objective": "keep provider state visible",
        "status": "running",
        "kind": "generic",
        "metadata": {"provider_blocked_at": "2026-05-01T00:00:00+00:00"},
    }
    snapshot = {
        "job_id": "job_demo",
        "job": job,
        "jobs": [job],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [],
        "summary_events": [],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=30)

    assert "blocked" in frame
    assert "Provider" in frame
    assert "action needed" in frame
    assert "advancing" not in frame


def test_chat_status_page_surfaces_context_pressure():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "context job",
            "objective": "keep context pressure visible",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "context job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
        "context_length": 8192,
        "token_usage": {"calls": 3, "latest_prompt_tokens": 7000, "total_tokens": 9000, "completion_tokens": 2000},
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=30)

    assert "Context" in frame
    assert "7.0K/8.2K" in frame
    assert "85%" in frame
    assert "high" in frame


def test_chat_status_page_surfaces_low_durable_yield():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "yield job",
            "objective": "keep durable progress visible",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [{"id": "job_demo", "title": "yield job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [{"id": "art_demo", "title": "Only Saved Output"}],
        "memory_entries": [],
        "events": [],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
        "counts": {"steps": 120, "artifacts": 1, "memory": 0},
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=30)

    assert "Yield" in frame
    assert "watch" in frame
    assert "120.0 actions/outcome" in frame


def test_chat_status_page_shows_job_outputs():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "show created outputs per job",
            "status": "running",
            "kind": "generic",
            "metadata": {},
        },
        "jobs": [
            {"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}},
            {"id": "job_other", "title": "other job", "status": "queued", "kind": "generic", "metadata": {}},
        ],
        "steps": [],
        "artifacts": [
            {"id": "art_demo", "title": "Primary Saved Draft"},
            {"id": "art_second", "title": "Secondary Saved Note"},
        ],
        "job_artifacts": {
            "job_demo": [
                {"id": "art_demo", "title": "Primary Saved Draft"},
                {"id": "art_second", "title": "Secondary Saved Note"},
            ],
            "job_other": [{"id": "art_other", "title": "Other Job Deliverable"}],
        },
        "job_counts": {
            "job_demo": {"artifacts": 2},
            "job_other": {"artifacts": 4},
        },
        "job_summary_events": {
            "job_demo": [
                {"event_type": "artifact", "title": "Primary Saved Draft", "body": "", "metadata": {}},
                {"event_type": "experiment", "title": "Primary quality check", "body": "", "metadata": {"metric_name": "score", "metric_value": 8}},
            ],
            "job_other": [
                {"event_type": "finding", "title": "Other job durable finding", "body": "", "metadata": {}},
            ],
        },
        "memory_entries": [],
        "events": [],
        "summary_events": [
            {"event_type": "finding", "title": "Latest durable milestone", "body": "", "metadata": {}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
        "counts": {"steps": 0, "artifacts": 1, "memory": 0},
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=34)

    assert "Jobs" in frame
    assert "Latest hour" in frame
    assert "1 findings" in frame
    assert "Outcome" in frame
    assert "Latest durable milestone" in frame
    assert "2 outputs" in frame
    assert "Primary Saved Draft" in frame
    assert "Secondary Saved Note" in frame
    assert "Primary quality check" in frame
    assert "4 outputs" in frame
    assert "Other Job Deliverable" in frame
    assert "Other job durable finding" in frame


def test_frame_snapshot_keeps_summary_events_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("keep frame refresh focused", title="focused")
        for index in range(30):
            db.append_event(
                job_id=job_id,
                event_type="tool_result",
                title="web_search",
                body=f"search noise {index}",
                metadata={"status": "completed"},
            )
        db.append_event(
            job_id=job_id,
            event_type="tool_result",
            title="write_file",
            body="write_file overwrite /tmp/paper.md",
            metadata={"status": "completed", "input": {"arguments": {"path": "/tmp/paper.md"}}, "output": {"path": "/tmp/paper.md"}},
        )
        db.append_event(
            job_id=job_id,
            event_type="tool_result",
            title="shell_exec",
            body="shell_exec rc=0",
            metadata={"status": "completed", "input": {"arguments": {"command": "printf draft | tee /tmp/outline.md"}}},
        )
        db.append_event(job_id=job_id, event_type="artifact", title="Durable Paper Draft", body="", metadata={})
        db.append_event(job_id=job_id, event_type="finding", title="Actual finding", body="", metadata={})
    finally:
        db.close()

    snapshot = _load_frame_snapshot(job_id, history_limit=4)
    summary_text = "\n".join(str(event.get("title") or event.get("body") or "") for event in snapshot["summary_events"])

    assert "Durable Paper Draft" in summary_text
    assert "Actual finding" in summary_text
    assert "write_file" in summary_text
    assert "shell_exec" in summary_text
    assert "web_search" not in summary_text
    assert "search noise" not in summary_text


def test_frame_snapshot_respects_explicit_job_over_saved_focus(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        focused_id = db.create_job("saved focus", title="saved focus")
        requested_id = db.create_job("requested focus", title="requested focus")
        (tmp_path / "shell_state.json").write_text(json.dumps({"focus_job_id": focused_id}), encoding="utf-8")
    finally:
        db.close()

    snapshot = _load_frame_snapshot(requested_id, history_limit=4)

    assert snapshot["job_id"] == requested_id
    assert snapshot["job"]["title"] == "requested focus"


def test_chat_status_page_marks_deferred_jobs_waiting():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "deferred job",
            "objective": "check a long-running process later",
            "status": "running",
            "kind": "generic",
            "metadata": {"defer_until": "2999-01-01T00:00:00+00:00", "defer_reason": "external process running"},
        },
        "jobs": [
            {
                "id": "job_demo",
                "title": "deferred job",
                "status": "running",
                "kind": "generic",
                "metadata": {"defer_until": "2999-01-01T00:00:00+00:00", "defer_reason": "external process running"},
            }
        ],
        "steps": [],
        "artifacts": [],
        "job_artifacts": {},
        "memory_entries": [],
        "events": [],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=132, height=28)

    assert "waiting" in frame
    assert "Wait" in frame
    assert "next check" in frame
    assert "external" in frame
    assert "active" not in frame


def test_chat_frame_collapses_repeated_failures_and_hides_memory_noise():
    repeated_error = {
        "event_type": "error",
        "title": "llm",
        "body": "Error code: 403 - {'error': {'message': 'Key limit exceeded (total limit).'}}",
        "metadata": {},
    }
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "stay readable",
            "status": "running",
            "kind": "generic",
            "metadata": {"task_queue": []},
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [{}],
        "events": [
            repeated_error,
            {
                "event_type": "compaction",
                "title": "rolling_state",
                "body": "very long compact memory " * 80,
                "metadata": {},
            },
            repeated_error,
            repeated_error,
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
        "counts": {"steps": 3, "artifacts": 0, "memory": 1},
    }

    frame = _build_chat_frame(snapshot, "", [], width=120, height=24, right_view="work")

    assert "FAIL x3" in frame
    assert "very long compact memory" not in frame


def test_work_pane_uses_badges_without_duplicate_action_verbs():
    snapshot = {
        "job_id": "job_demo",
        "job": {
            "id": "job_demo",
            "title": "demo job",
            "objective": "stay readable",
            "status": "running",
            "kind": "generic",
            "metadata": {"task_queue": []},
        },
        "jobs": [{"id": "job_demo", "title": "demo job", "status": "running", "kind": "generic", "metadata": {}}],
        "steps": [],
        "artifacts": [],
        "memory_entries": [],
        "events": [
            {"event_type": "artifact", "title": "Demo Output", "body": "", "metadata": {}},
            {"event_type": "finding", "title": "Demo Finding", "body": "", "metadata": {}},
            {"event_type": "experiment", "title": "Demo Measurement", "body": "", "metadata": {}},
        ],
        "daemon": {"running": True, "metadata": {"pid": 123}},
        "model": "model/demo",
    }

    frame = _build_chat_frame(snapshot, "", [], width=120, height=24, right_view="work")

    assert "save Demo Output" in frame
    assert "find Demo Finding" in frame
    assert "test Demo Measurement" in frame
    assert "save saved" not in frame
    assert "find finding" not in frame
    assert "test experiment" not in frame


def test_run_reopens_completed_focused_job(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    parser = build_parser()
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Keep improving", title="perpetual")
        db.update_job_status(job_id, "completed")
    finally:
        db.close()
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)
    args = parser.parse_args(["run", "perpetual", "--no-follow"])

    args.func(args)

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "focus set: perpetual" in out
        assert job["status"] == "queued"
        assert job["metadata"]["last_note"] == "reopened from completed by operator run command"
        assert started["poll_seconds"] == 0.0
    finally:
        db.close()


def test_create_sets_new_job_as_shell_focus(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    parser = build_parser()
    args = parser.parse_args(["create", "Research new topic", "--title", "new research", "--kind", "generic"])

    args.func(args)
    created = capsys.readouterr().out.strip()
    assert _run_shell_line("focus") is True

    out = capsys.readouterr().out
    assert created == "created new research"
    assert "new research" in out
    assert (tmp_path / "jobs" / "new-research" / "program.md").exists()
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job("new-research")
        assert job["status"] == "queued"
        assert job["metadata"]["planning_status"] == "auto_accepted"
        assert job["metadata"]["planning"]["questions"]
        tasks = job["metadata"]["task_queue"]
        assert tasks
        assert all(task["output_contract"] for task in tasks)
        assert all(task["acceptance_criteria"] for task in tasks)
        assert all(task["evidence_needed"] for task in tasks)
        assert all(task["stall_behavior"] for task in tasks)
    finally:
        db.close()


def test_commands_accept_unquoted_job_titles_in_shell(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Research topic", title="nightly research")
    finally:
        db.close()

    assert _run_shell_line("status nightly research") is True

    out = capsys.readouterr().out
    assert "focus: nightly research" in out
    assert "state: open" in out
    assert "job_" not in out


def test_shell_stop_job_title_pauses_job_instead_of_stopping_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        db.update_job_status(job_id, "running")
    finally:
        db.close()

    assert _run_shell_line("stop nightly research") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "stopped nightly research" in out
        assert job["status"] == "paused"
        assert job["metadata"]["last_note"] == "stopped by operator"
    finally:
        db.close()


def test_resume_clears_provider_block_before_retry(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        db.update_job_status(
            job_id,
            "paused",
            metadata_patch={"provider_blocked_at": "2026-05-01T00:00:00+00:00"},
        )
    finally:
        db.close()

    main(["resume", "nightly research"])

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "resumed nightly research" in out
        assert job["status"] == "queued"
        assert job["metadata"]["provider_blocked_at"] == ""
        assert job["metadata"]["provider_unblocked_at"]
    finally:
        db.close()


def test_shell_cancel_prefers_multiword_job_title_over_note(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        db.update_job_status(job_id, "running")
    finally:
        db.close()

    assert _run_shell_line("cancel nightly research") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "cancelled nightly research" in out
        assert ": finder" not in out
        assert job["status"] == "cancelled"
        assert "last_note" not in job["metadata"]
    finally:
        db.close()


def test_shell_pause_splits_note_after_longest_matching_job_title(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        db.update_job_status(job_id, "running")
    finally:
        db.close()

    assert _run_shell_line("pause nightly research checking costs") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "paused nightly research: checking costs" in out
        assert job["status"] == "paused"
        assert job["metadata"]["last_note"] == "checking costs"
    finally:
        db.close()


def test_chat_handle_line_adds_operator_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()

    assert (
        _chat_handle_line(
            job_id, "prefer artifact-backed findings", reply_fn=lambda _job_id, _message: "Okay, I will focus there."
        )
        is True
    )

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "waiting:" in out
        assert "Okay, I will focus there." in out
        assert job["metadata"]["operator_messages"][-1]["source"] == "chat"
        assert job["metadata"]["operator_messages"][-1]["message"] == "prefer artifact-backed findings"
        assert job["metadata"]["last_agent_update"]["category"] == "chat"
    finally:
        db.close()


def test_chat_can_spawn_new_job_from_plain_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        original_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)

    assert (
        _chat_handle_line(
            original_id,
            "create a job to monitor nightly benchmarks and report regressions",
            reply_fn=lambda _job_id, _message: "should not call model",
        )
        is True
    )

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        jobs = db.list_jobs()
        assert len(jobs) == 2
        created = [job for job in jobs if job["id"] != original_id][0]
        assert "monitor nightly benchmarks" in created["objective"]
        assert created["status"] == "queued"
        assert created["metadata"]["planning_status"] == "auto_accepted"
        assert "should not call model" not in out
        assert "Created job" in out
        assert "Started worker" in out
        assert started["poll_seconds"] == 0.0
        assert started["quiet"] is True
    finally:
        db.close()


def test_chat_can_queue_new_job_without_starting(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        original_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)

    assert (
        _chat_handle_line(
            original_id,
            "create only a job to monitor nightly benchmarks and report regressions",
            reply_fn=lambda _job_id, _message: "should not call model",
        )
        is True
    )

    out = capsys.readouterr().out
    assert "Created job" in out
    assert "Started worker" not in out
    assert started == {}


def test_chat_can_spawn_generic_deliverable_job_from_plain_message(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        original_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)

    assert (
        _chat_handle_line(
            original_id,
            "generate a polished launch checklist for this repository",
            reply_fn=lambda _job_id, _message: "should not call model",
        )
        is True
    )

    db = AgentDB(tmp_path / "state.db")
    try:
        jobs = db.list_jobs()
        assert len(jobs) == 2
        created = [job for job in jobs if job["id"] != original_id][0]
        assert "launch checklist" in created["objective"]
        assert started["quiet"] is True
    finally:
        db.close()


def test_chat_start_job_message_starts_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        original_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)

    assert (
        _chat_handle_line(
            original_id,
            "start a job to monitor nightly benchmarks and report regressions",
            reply_fn=lambda _job_id, _message: "should not call model",
        )
        is True
    )

    out = capsys.readouterr().out
    assert started["poll_seconds"] == 0.0
    assert started["quiet"] is True
    assert "Started worker" in out


def test_chat_create_job_and_run_it_starts_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        original_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr("nipux_cli.cli._start_daemon_if_needed", fake_start)

    assert (
        _chat_handle_line(
            original_id,
            "create a job to monitor nightly benchmarks and then run it",
            reply_fn=lambda _job_id, _message: "should not call model",
        )
        is True
    )

    out = capsys.readouterr().out
    assert started["poll_seconds"] == 0.0
    assert started["quiet"] is True
    assert "Started worker" in out


def test_chat_jobs_command_lists_jobs_instead_of_steering(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/jobs", reply_fn=lambda _job_id, _message: "should not run") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "nightly research" in out
        assert "should not run" not in out
        assert job["metadata"].get("operator_messages") is None
    finally:
        db.close()


def test_chat_command_inside_chat_is_not_queued(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()

    assert (
        _chat_handle_line(job_id, 'chat "nightly research"', reply_fn=lambda _job_id, _message: "should not run")
        is True
    )

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "already chatting with nightly research" in out
        assert "should not run" not in out
        assert job["metadata"].get("operator_messages") is None
    finally:
        db.close()


def test_chat_run_accepts_initial_plan_before_starting(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    parser = build_parser()
    args = parser.parse_args(["create", "Research new topic", "--title", "new research", "--kind", "generic"])
    args.func(args)
    job_id = "new-research"
    captured = {}

    def fake_run(run_args):
        captured["job_id"] = run_args.job_id

    monkeypatch.setattr("nipux_cli.cli.cmd_run", fake_run)

    assert _chat_handle_line(job_id, "/run") is True

    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert job["status"] == "queued"
        assert captured["job_id"] == job_id
    finally:
        db.close()


def test_build_chat_messages_includes_recent_job_state(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research", kind="generic")
        db.create_job("Monitor another branch", title="other branch", kind="generic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
        db.finish_step(step_id, status="completed", summary="web_search returned useful sources")
        job = db.get_job(job_id)

        messages = _build_chat_messages(db, job, "what is going on?")

        content = messages[-1]["content"]
        assert "Job title: nightly research" in content
        assert "Jobs:" in content
        assert "* 1. nightly research" in content
        assert "- 2. other branch" in content
        assert "web_search returned useful sources" in content
        assert "what is going on?" in content
    finally:
        db.close()


def test_build_chat_messages_includes_durable_outcome_summary(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research", kind="generic")
        db.append_event(job_id=job_id, event_type="artifact", title="First draft", body="saved report", metadata={})
        db.append_event(job_id=job_id, event_type="finding", title="Evidence map", body="", metadata={})
        db.append_event(
            job_id=job_id,
            event_type="experiment",
            title="Citation coverage",
            body="",
            metadata={"metric_name": "citations", "metric_value": 12, "metric_unit": "count"},
        )
        job = db.get_job(job_id)

        messages = _build_chat_messages(db, job, "what has it actually done?")

        content = messages[-1]["content"]
        assert "Durable outcomes:" in content
        assert "summary: 1 outputs 1 findings 1 measurements" in content
        assert "save: First draft" in content
        assert "find: Evidence map" in content
        assert "test: Citation coverage" in content
    finally:
        db.close()


def test_build_chat_messages_does_not_include_local_machine_context(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host private-box\n  HostName 10.9.8.7\n  User private\n", encoding="utf-8")
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research", kind="generic")
        job = db.get_job(job_id)

        messages = _build_chat_messages(db, job, "what is going on?")

        content = messages[-1]["content"]
        assert "Local CLI context" not in content
        assert "private-box" not in content
        assert "10.9.8.7" not in content
    finally:
        db.close()


def test_build_chat_messages_points_to_artifact_and_lessons(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research", kind="generic")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        ArtifactStore(tmp_path, db=db).write_text(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            title="Findings Batch",
            summary="15 reusable findings",
            content="Acme",
        )
        db.append_lesson(job_id, "Prefer actual evidence sources over low-evidence pages.", category="strategy")
        job = db.get_job(job_id)

        messages = _build_chat_messages(db, job, "where are the findings?")

        content = messages[-1]["content"]
        assert "/artifact 1" in content
        assert "Prefer actual evidence sources over low-evidence pages" in content
    finally:
        db.close()


def test_build_chat_messages_clip_large_visible_state(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research", kind="generic")
        for index in range(30):
            db.append_event(
                job_id=job_id,
                event_type="finding",
                title=f"large finding {index}",
                body="evidence " * 400,
                metadata={},
            )
        job = db.get_job(job_id)

        messages = _build_chat_messages(db, job, "keep this exact operator question visible")

        content = messages[-1]["content"]
        assert len(content) < 14_000
        assert "clipped" in content
        assert "keep this exact operator question visible" in content
    finally:
        db.close()


def test_artifact_command_resolves_title_query(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        ArtifactStore(tmp_path, db=db).write_text(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            title="Findings Batch",
            summary="saved findings",
            content="Acme Corp\n",
        )
    finally:
        db.close()

    parser = build_parser()
    args = parser.parse_args(["artifact", "Findings", "Batch"])
    args.func(args)

    out = capsys.readouterr().out
    assert "artifact: Findings Batch" in out
    assert "Acme Corp" in out


def test_artifacts_command_prints_compact_view_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        ArtifactStore(tmp_path, db=db).write_text(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            title="Findings Batch",
            summary="saved findings",
            content="Acme Corp\n",
        )
    finally:
        db.close()

    parser = build_parser()
    args = parser.parse_args(["artifacts"])
    args.func(args)

    out = capsys.readouterr().out
    assert "saved outputs nightly research" in out
    assert "view: artifact 1" in out
    assert "/jobs/" not in out


def test_artifact_command_opens_recent_output_by_number(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="write_artifact")
        ArtifactStore(tmp_path, db=db).write_text(
            job_id=job_id,
            run_id=run_id,
            step_id=step_id,
            title="Findings Batch",
            summary="saved findings",
            content="Acme Corp\n",
        )
    finally:
        db.close()

    parser = build_parser()
    args = parser.parse_args(["artifact", "1"])
    args.func(args)

    out = capsys.readouterr().out
    assert "artifact: Findings Batch" in out
    assert "Acme Corp" in out


def test_chat_work_defaults_to_compact_output(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()
    captured = {}

    def fake_work(args):
        captured["verbose"] = args.verbose
        captured["chars"] = args.chars

    monkeypatch.setattr("nipux_cli.cli.cmd_work", fake_work)

    assert _chat_handle_line(job_id, "/work") is True

    assert captured == {"verbose": False, "chars": 260}


def test_chat_learn_adds_lesson(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/learn low-evidence pages are not research findings") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        assert "learned for nightly research" in out
        assert job["metadata"]["last_lesson"]["lesson"] == "low-evidence pages are not research findings"
    finally:
        db.close()


def test_chat_follow_queues_follow_up_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
    finally:
        db.close()

    assert _chat_handle_line(job_id, "/follow after this branch, check another source") is True

    out = capsys.readouterr().out
    db = AgentDB(tmp_path / "state.db")
    try:
        job = db.get_job(job_id)
        message = job["metadata"]["operator_messages"][-1]
        assert "waiting after current branch" in out
        assert message["mode"] == "follow_up"
        assert message["message"] == "after this branch, check another source"
    finally:
        db.close()


def test_findings_sources_memory_metrics_commands(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="nightly research")
        db.append_finding_record(job_id, name="Acme Finding", category="example category", score=0.8)
        db.append_task_record(job_id, title="Explore primary sources", status="open", priority=5)
        db.append_experiment_record(job_id, title="Variant A", status="measured", metric_name="score", metric_value=1.5)
        db.append_source_record(job_id, "https://example.com", usefulness_score=0.9, yield_count=1)
        db.append_lesson(job_id, "Source indexes work.", category="strategy")
        db.append_reflection(job_id, "Keep using source indexes.", strategy="Try primary records.")
    finally:
        db.close()

    parser = build_parser()
    for command in (["findings"], ["tasks"], ["experiments"], ["sources"], ["memory"], ["metrics"]):
        args = parser.parse_args(command)
        args.func(args)

    out = capsys.readouterr().out
    assert "Acme Finding" in out
    assert "Explore primary sources" in out
    assert "Variant A" in out
    assert "https://example.com" in out
    assert "Keep using source indexes" in out
    assert "tasks: 1" in out
    assert "experiments: 1" in out
    assert "findings: 1" in out


def test_shell_natural_update_phrase_shows_updates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _run_shell_line("tell me updates") is True
    assert _run_shell_line("show outcomes") is True

    out = capsys.readouterr().out
    assert "updates" in out
    assert "queued for" not in out


def test_updates_command_summarizes_durable_outcomes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
        artifact_path = tmp_path / "artifact.md"
        artifact_path.write_text("saved", encoding="utf-8")
        db.append_event(
            job_id,
            event_type="tool_call",
            title="web_search",
            metadata={"input": {"arguments": {"query": "raw search"}}},
        )
        db.append_finding_record(job_id, name="Durable Result", category="evidence", reason="real outcome", score=0.7)
        db.add_artifact(
            job_id=job_id,
            path=artifact_path,
            sha256="abc",
            artifact_type="text",
            title="Saved Report",
            summary="durable output",
        )
    finally:
        db.close()

    args = build_parser().parse_args(["updates", "research", "--limit", "3", "--chars", "120"])
    args.func(args)

    out = capsys.readouterr().out
    assert "outcomes by hour:" in out
    assert "Durable Result" in out
    assert "Saved Report" in out
    assert "latest saved outputs:" in out
    assert "raw tool stream: activity" in out
    assert "recent tool calls:" not in out


def test_updates_all_summarizes_durable_work_across_jobs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        first_id = db.create_job("Research first topic", title="first")
        second_id = db.create_job("Research second topic", title="second")
        first_path = tmp_path / "first.md"
        first_path.write_text("first", encoding="utf-8")
        second_path = tmp_path / "second.md"
        second_path.write_text("second", encoding="utf-8")
        db.append_finding_record(first_id, name="First durable finding", category="evidence")
        db.add_artifact(
            job_id=first_id,
            path=first_path,
            sha256="abc",
            artifact_type="text",
            title="First saved output",
            summary="first summary",
        )
        db.append_experiment_record(
            second_id,
            title="Second measured result",
            status="measured",
            metric_name="quality",
            metric_value=9,
            metric_unit="points",
        )
        db.add_artifact(
            job_id=second_id,
            path=second_path,
            sha256="def",
            artifact_type="text",
            title="Second saved output",
            summary="second summary",
        )
    finally:
        db.close()

    args = build_parser().parse_args(["outcomes", "--all", "--limit", "5", "--chars", "120"])
    args.func(args)

    out = capsys.readouterr().out
    assert "outcomes all jobs | 2 tracked" in out
    assert "first |" in out
    assert "second |" in out
    assert "First durable finding" in out
    assert "First saved output" in out
    assert "Second measured result" in out
    assert "Second saved output" in out


def test_history_and_events_commands_render_visible_timeline(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Research topic", title="research")
        db.append_operator_message(job_id, "operator timeline note", source="test")
        db.append_agent_update(job_id, "agent timeline note", category="chat")
    finally:
        db.close()

    parser = build_parser()
    parser.parse_args(["history", "research"]).func(parser.parse_args(["history", "research"]))
    parser.parse_args(["events", "research"]).func(parser.parse_args(["events", "research"]))

    out = capsys.readouterr().out
    assert "history research" in out
    assert "events research" in out
    assert "operator timeline note" in out
    assert "agent timeline note" in out


def test_shell_natural_health_phrase_shows_health(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        db.create_job("Research topic", title="research")
    finally:
        db.close()

    assert _run_shell_line("is it running") is True

    out = capsys.readouterr().out
    assert "Nipux Health" in out
    assert "queued for" not in out


def test_health_prints_recent_daemon_events(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    config = load_config()
    append_daemon_event(config, "daemon_error", error_type="RuntimeError", error="provider fell over")

    parser = build_parser()
    args = parser.parse_args(["health", "--limit", "3"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Nipux Health" in out
    assert "daemon_error" in out
    assert "RuntimeError" in out


def test_launch_agent_plist_contains_daemon_command(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    plist = _launch_agent_plist(poll_seconds=7, quiet=True)

    assert "com.nipux.agent" in plist
    assert "<string>daemon</string>" in plist
    assert "<string>--poll-seconds</string>" in plist
    assert "<string>7</string>" in plist
    assert str(tmp_path) in plist


def test_systemd_service_text_contains_daemon_command(monkeypatch, tmp_path):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))

    service = _systemd_service_text(poll_seconds=0, quiet=True)

    assert "[Service]" in service
    assert "ExecStart=" in service
    assert "daemon --poll-seconds 0" in service
    assert f"Environment=NIPUX_HOME={tmp_path}" in service
    assert "Restart=always" in service

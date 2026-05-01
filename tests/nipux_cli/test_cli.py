from nipux_cli.artifacts import ArtifactStore
from nipux_cli import __version__
from nipux_cli.cli import (
    CHAT_SLASH_COMMANDS,
    FIRST_RUN_SLASH_COMMANDS,
    _autocomplete_slash,
    _build_first_run_frame,
    _build_chat_frame,
    _build_chat_messages,
    _chat_handle_line,
    _chat_control_command,
    _config_field_value,
    _decode_terminal_escape,
    _first_run_click_action,
    _frame_next_job_id,
    _handle_first_run_menu_line,
    _handle_first_run_frame_line,
    _handle_chat_message,
    _inline_setting_notice,
    _launch_agent_plist,
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

    def fake_input(_prompt):
        return "new Build a durable workflow"

    def fake_enter_chat(job_id, *, show_history, history_limit):
        opened["job_id"] = job_id
        opened["show_history"] = show_history
        opened["history_limit"] = history_limit
        print(f"opened {job_id}")

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("nipux_cli.cli._enter_chat", fake_enter_chat)

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
    assert "created Build a durable workflow" in out
    assert "Opening workspace" in out
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
    assert "Control" in frame
    assert "Compose" in frame
    assert "New job" in frame
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
    assert "tab completes first match" in frame


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
    assert _autocomplete_slash("/step", CHAT_SLASH_COMMANDS) == "/step-limit "
    assert _autocomplete_slash("/out", FIRST_RUN_SLASH_COMMANDS) == "/output-chars "
    assert _autocomplete_slash("plain text", CHAT_SLASH_COMMANDS) == "plain text"
    lines = _slash_suggestion_lines("/art", CHAT_SLASH_COMMANDS, width=80)
    text = "\n".join(lines)
    assert "/artifacts" in text
    assert "/artifact" in text
    assert "/run" not in text
    assert "/shell" not in "\n".join(_slash_suggestion_lines("/", CHAT_SLASH_COMMANDS, width=80, limit=20))


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
    assert "/model MODEL" in out
    assert "/api-key KEY" in out
    assert "/timeout SECONDS" in out
    assert "/home PATH" in out
    assert "/digest-time HH:MM" in out
    assert "/shell" not in out


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
    assert "saved model.request_timeout_seconds = 45.0" in out
    assert "saved runtime.max_step_seconds = 90" in out
    assert "saved runtime.artifact_inline_char_limit = 4096" in out
    assert "saved runtime.daily_digest_enabled = False" in out
    assert "saved runtime.daily_digest_time = 08:30" in out
    assert "saved model.api_key_env = NIPUX_TEST_KEY" in out
    assert "saved NIPUX_TEST_KEY" in out
    assert "sk-test-value" not in out
    assert _config_field_value("model.name") == "provider/model"
    assert _config_field_value("model.base_url") == "https://example.com/v1"
    assert _config_field_value("model.context_length") == 8192
    assert _config_field_value("model.request_timeout_seconds") == 45.0
    assert _config_field_value("runtime.max_step_seconds") == 90
    assert _config_field_value("runtime.artifact_inline_char_limit") == 4096
    assert _config_field_value("runtime.daily_digest_enabled") is False
    assert _config_field_value("runtime.daily_digest_time") == "08:30"
    assert "NIPUX_TEST_KEY=sk-test-value" in (tmp_path / ".env").read_text(encoding="utf-8")


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
            "metadata": {"task_queue": [{"status": "open"}]},
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
            "cost": 0.0123,
            "has_cost": True,
        },
    }

    frame = _build_chat_frame(snapshot, "hello", [], width=100, height=22)

    assert len(frame.splitlines()) <= 22
    assert "Nipux CLI" in frame
    assert "Chat" in frame
    assert "Status" in frame
    assert "Latest" in frame
    assert "Jobs" in frame
    assert "Saved outputs" in frame
    assert "ctx" in frame
    assert "4.1K/8.2K" in frame
    assert "out" in frame
    assert "1.2K" in frame
    assert "$0.0123" in frame
    assert "Compose" in frame
    assert "❯ hello" in frame

    work = _build_chat_frame(snapshot, "", [], width=100, height=24, right_view="work")
    assert "Work" in work
    assert "Tool / console" in work
    assert "search demo" in work

    updates = _build_chat_frame(snapshot, "", [], width=100, height=24, right_view="updates")
    assert "Progress" in updates
    assert "Progress by hour" in updates

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
    assert "A persistent agent workspace." in frame
    assert "No chat yet." not in frame


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
    assert _chat_control_command("start working") == "/run"
    assert _chat_control_command("pause this job") == "/pause"
    assert _chat_control_command("show jobs") == "/jobs"
    assert _chat_control_command("change model") == "/model"


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

    assert "Research Paper Draft" in frame
    assert "Distillation finding" in frame
    assert "Compare methods" in frame
    assert "Paper roadmap" in frame
    assert "passed Draft" in frame
    assert "Citation coverage check" in frame
    assert "learned strategy" in frame
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

    assert "Progress" in frame
    assert "Progress by hour" in frame
    assert "distillation agents" in frame
    assert "Literature Review Draft" in frame
    assert "Trajectory distillation" in frame
    assert "Citation density check" in frame
    assert "paper.md" in frame
    assert "outline.md" in frame


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
        "artifacts": [{"id": "art_demo", "title": "Primary Saved Draft"}],
        "job_artifacts": {
            "job_demo": [{"id": "art_demo", "title": "Primary Saved Draft"}],
            "job_other": [{"id": "art_other", "title": "Other Job Deliverable"}],
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
    assert "Outcome" in frame
    assert "Latest durable milestone" in frame
    assert "Primary Saved Draft" in frame
    assert "Other Job Deliverable" in frame


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
        run_id = db.start_run(job_id, model="fake")
        step_id = db.add_step(job_id=job_id, run_id=run_id, kind="tool", tool_name="web_search")
        db.finish_step(step_id, status="completed", summary="web_search returned useful sources")
        job = db.get_job(job_id)

        messages = _build_chat_messages(db, job, "what is going on?")

        content = messages[-1]["content"]
        assert "Job title: nightly research" in content
        assert "web_search returned useful sources" in content
        assert "what is going on?" in content
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
        db.append_lesson(job_id, "Directories work.", category="strategy")
        db.append_reflection(job_id, "Keep using directories.", strategy="Try chambers.")
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
    assert "Keep using directories" in out
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

    out = capsys.readouterr().out
    assert "updates" in out
    assert "queued for" not in out


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

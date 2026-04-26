import json

from nipux_cli.browser import _annotate_source_quality, _session_name, _socket_dir
from nipux_cli.tools import DEFAULT_REGISTRY, ToolContext
from nipux_cli.web import _strip_html
from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import AppConfig, RuntimeConfig
from nipux_cli.db import AgentDB


def test_session_name_is_stable_and_safe():
    assert _session_name("job_abc/def") == "nipux_job_abc_def"


def test_long_session_name_is_short_and_hashed():
    task_id = "research-a-very-long-objective-title-that-needs-a-short-browser-session-name"
    name = _session_name(task_id)
    socket_dir = _socket_dir(task_id)

    assert name.startswith("nipux_research-a-very-long")
    assert len(name) <= 37
    assert len(str(socket_dir)) < 80


def test_strip_html_removes_scripts_and_keeps_text():
    text = _strip_html("<html><script>bad()</script><h1>Hello</h1><p>World</p></html>")
    assert "bad" not in text
    assert "Hello" in text
    assert "World" in text


def test_browser_marks_anti_bot_interstitial_as_warning():
    result = _annotate_source_quality({
        "success": True,
        "data": {"title": "Just a moment...", "url": "https://clutch.co/example"},
        "snapshot": "Performing security verification. Cloudflare security challenge.",
    })

    assert result["success"] is True
    assert "error" not in result
    assert result["source_warning"] == "cloudflare anti-bot challenge"
    assert result["warnings"][0]["type"] == "anti_bot"


def test_browser_marks_captcha_block_as_warning():
    result = _annotate_source_quality({
        "success": True,
        "data": {"title": "Source search", "url": "https://source.example/search"},
        "snapshot": 'Iframe "Security CAPTCHA" You have been blocked. You are browsing and clicking at a speed much faster than expected.',
    })

    assert result["source_warning"] == "captcha/anti-bot block"
    assert result["warnings"][0]["type"] == "anti_bot"


def test_web_extract_marks_anti_bot_pages_as_warning(monkeypatch):
    from nipux_cli import web

    def fake_request(url):
        del url
        return "<h1>Performing security verification</h1><p>Cloudflare security challenge</p>", "text/html"

    monkeypatch.setattr(web, "_request", fake_request)
    result = web.web_extract(["https://clutch.co/example"])

    page = result["pages"][0]
    assert "error" not in page
    assert page["source_warning"] == "cloudflare anti-bot challenge"
    assert page["warnings"][0]["type"] == "anti_bot"
    assert "Cloudflare security challenge" in page["text"]


def test_browser_tool_uses_native_wrapper(monkeypatch, tmp_path):
    from nipux_cli import browser

    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Browse")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id)

        def fake_navigate(cfg, *, task_id, url):
            return {"success": True, "task_id": task_id, "url": url}

        monkeypatch.setattr(browser, "navigate", fake_navigate)
        result = json.loads(DEFAULT_REGISTRY.handle("browser_navigate", {"url": "https://example.com"}, ctx))

        assert result == {"success": True, "task_id": job_id, "url": "https://example.com"}
    finally:
        db.close()


def test_browser_click_adds_recovery_snapshot_for_stale_ref(monkeypatch, tmp_path):
    from nipux_cli import browser

    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))
    calls = []

    def fake_command(cfg, *, task_id, command, args=None, timeout=60):
        del cfg, task_id, args, timeout
        calls.append(command)
        if command == "click":
            return {"success": False, "error": "Unknown ref: e102"}
        return {
            "success": True,
            "data": {
                "snapshot": "Directory",
                "refs": {"e1": {"role": "link", "name": "New Result"}},
            },
        }

    monkeypatch.setattr(browser, "run_browser_command", fake_command)
    result = browser.click(config, task_id="job_abc", ref="@e102")

    assert calls == ["click", "snapshot"]
    assert result["success"] is False
    assert result["recovery_snapshot"]["data"]["refs"]["e1"]["name"] == "New Result"
    assert "stale" in result["recovery_guidance"]

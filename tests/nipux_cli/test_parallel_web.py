"""Exercise the optional backend through the real MCP SDK and tool dispatcher."""

import asyncio
import builtins
import hashlib
import json
from dataclasses import replace

import pytest

from nipux_cli import parallel_web, web
from nipux_cli.artifacts import ArtifactStore
from nipux_cli.config import ToolAccessConfig, load_config
from nipux_cli.db import AgentDB
from nipux_cli.llm import LLMResponse, ScriptedLLM, ToolCall
from nipux_cli.tools import DEFAULT_REGISTRY, ToolContext
from nipux_cli.worker import run_one_step


httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp")


SEARCH_DATA = {"results": [{
    "title": "Example source", "url": "https://example.com/source?q=a%20b&n=1",
    "excerpts": ["Source evidence"],
}]}
FETCH_DATA = {"results": [{"url": "https://example.com/source", "full_content": "# Evidence\n" + "text " * 3000}]}


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    # Ambient model and Parallel secrets must never become MCP credentials.
    monkeypatch.setenv("OPENAI_API_KEY", "model-test-secret")
    monkeypatch.setenv("PARALLEL_API_KEY", "parallel-test-secret")
    (tmp_path / ".env").write_text("PROVIDER_API_KEY=saved-test-secret\n")
    (tmp_path / "config.yaml").write_text("tools:\n  search_provider: parallel\n")
    config = load_config()
    db = AgentDB(config.runtime.state_db_path)
    job_id = db.create_job("Collect evidence", title="evidence", kind="generic")
    ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id)
    try:
        yield ctx
    finally:
        db.close()


@pytest.fixture
def mcp_server(monkeypatch):
    requests = []
    state = {"search": SEARCH_DATA, "fetch": FETCH_DATA, "sse": False, "error": None}
    real_client = httpx.AsyncClient
    clients = []

    async def respond(request):
        requests.append(request)
        message = json.loads(request.content)
        method = message["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-server", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "inputSchema": {"type": "object"}} for name in ("web_search", "web_fetch")
            ]}
        elif method == "tools/call":
            if state.get("wait"):
                state["started"].set()
                await asyncio.Event().wait()
            if state.get("http_error"):
                return httpx.Response(state["http_error"], text="Service unavailable")
            if state["error"]:
                result = {"isError": True, "content": [{"type": "text", "text": state["error"]}]}
            else:
                data = state["search"] if message["params"]["name"] == "web_search" else state["fetch"]
                result = {"content": [{"type": "text", "text": json.dumps(data)}]}
                if state.get("structured", True):
                    result["structuredContent"] = data
        else:
            raise AssertionError(method)
        response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if state["sse"]:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text="event: message\ndata: " + json.dumps(response) + "\n\n",
            )
        return httpx.Response(200, json=response)

    def client(**kwargs):
        instance = real_client(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests, state, clients


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_registry_search_and_fetch_use_mcp_http(context, mcp_server, sse, structured):
    requests, state, clients = mcp_server
    state.update(sse=sse, structured=structured)
    search = json.loads(DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence", "limit": 1}, context))
    fetched = json.loads(DEFAULT_REGISTRY.handle("web_extract", {"urls": ["https://example.com/source"]}, context))

    assert search == {"success": True, "query": "source evidence", **SEARCH_DATA}
    assert fetched["success"] is True
    assert fetched["pages"][0]["text"] == FETCH_DATA["results"][0]["full_content"][:12000]
    assert fetched["pages"][0]["truncated"] is True
    assert all(client.is_closed for client in clients)
    for request in requests:
        assert str(request.url) == parallel_web.MCP_URL
        assert request.headers["user-agent"] == parallel_web.USER_AGENT
        assert "authorization" not in request.headers
        assert "secret" not in request.content.decode()
    messages = [json.loads(request.content) for request in requests]
    assert [m["method"] for m in messages].count("tools/list") == 2
    calls = [m["params"] for m in messages if m["method"] == "tools/call"]
    identity = f"{context.job_id}:{context.db.get_job(context.job_id)['created_at']}"
    session = hashlib.sha256(identity.encode()).hexdigest()
    assert calls == [
        {"name": "web_search", "arguments": {
            "objective": "source evidence", "search_queries": ["source evidence"], "session_id": session,
        }},
        {"name": "web_fetch", "arguments": {
            "urls": ["https://example.com/source"], "full_content": True, "session_id": session,
        }},
    ]


def test_session_identity_is_stable_after_restart_and_distinguishes_same_slug(context, mcp_server, tmp_path):
    requests, _, _ = mcp_server
    DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, context)
    other_path = tmp_path / "other.db"
    other_db = AgentDB(other_path)
    try:
        other_job = other_db.create_job("Collect evidence", title="evidence", kind="generic")
        assert other_job == context.job_id
        DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, replace(context, db=other_db))
    finally:
        other_db.close()
    reopened = AgentDB(other_path)
    try:
        DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, replace(context, db=reopened))
    finally:
        reopened.close()
    sessions = [
        json.loads(r.content)["params"]["arguments"]["session_id"]
        for r in requests if json.loads(r.content)["method"] == "tools/call"
    ]
    assert sessions[0] != sessions[1]
    assert sessions[1] == sessions[2]


@pytest.mark.parametrize("name,args", [
    ("web_search", {"query": "source evidence"}),
    ("web_extract", {"urls": ["https://example.com/source"]}),
])
def test_scripted_worker_consumes_and_persists_results(context, mcp_server, name, args):
    result = run_one_step(
        context.job_id, config=context.config, db=context.db,
        llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name=name, arguments=args)])]),
    )
    assert result.status == "completed"
    assert result.tool_name == name
    steps = context.db.list_steps(job_id=context.job_id)
    assert steps[-1]["output"]["success"] is True
    assert steps[-1]["output"]["results" if name == "web_search" else "pages"]
    job = context.db.get_job(context.job_id)
    assert "https://example.com/source" in json.dumps(job["metadata"]["source_ledger"])


def test_saved_default_and_explicit_provider_are_preserved(context, mcp_server, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(web, "web_search", lambda query, *, limit: calls.append(query) or {"success": True})
    (tmp_path / "config.yaml").write_text("tools:\n  web: true\n")
    ctx = replace(context, config=load_config())
    assert ctx.config.tools.search_provider == "duckduckgo"
    DEFAULT_REGISTRY.handle("web_search", {"query": "saved default"}, ctx)
    assert calls == ["saved default"]
    assert not mcp_server[0]
    (tmp_path / "config.yaml").write_text("tools:\n  search_provider: parallel\n")
    ctx = replace(context, config=load_config())
    assert json.loads(DEFAULT_REGISTRY.handle("web_search", {"query": "explicit choice"}, ctx))["success"]
    assert mcp_server[0]
    assert calls == ["saved default"]


def test_fresh_no_key_configuration_uses_duckduckgo(tmp_path, monkeypatch, mcp_server):
    monkeypatch.setenv("NIPUX_HOME", str(tmp_path))
    for name in ("OPENAI_API_KEY", "PARALLEL_API_KEY", "PROVIDER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = load_config()
    assert config.tools.search_provider == "duckduckgo"
    assert config.tools.web is True
    calls = []
    monkeypatch.setattr(web, "web_search", lambda query, *, limit: calls.append(query) or {"success": True})
    db = AgentDB(config.runtime.state_db_path)
    try:
        job_id = db.create_job("Collect evidence")
        ctx = ToolContext(config=config, db=db, artifacts=ArtifactStore(tmp_path, db), job_id=job_id)
        assert json.loads(DEFAULT_REGISTRY.handle("web_search", {"query": "fresh default"}, ctx))["success"]
        assert calls == ["fresh default"]
        assert not mcp_server[0]
    finally:
        db.close()


def test_missing_optional_dependency_is_actionable(context, monkeypatch):
    real_import = builtins.__import__

    def without_mcp(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("optional package absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)
    result = json.loads(DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, context))
    assert result["success"] is False
    assert "install nipux[parallel]" in result["error"]


def test_disabled_web_never_connects(context, mcp_server):
    ctx = replace(context, config=replace(context.config, tools=ToolAccessConfig(web=False, search_provider="parallel")))
    names = [tool["function"]["name"] for tool in DEFAULT_REGISTRY.openai_tools(ctx.config)]
    assert "web_search" not in names and "web_extract" not in names
    for name in ("web_search", "web_extract"):
        result = json.loads(DEFAULT_REGISTRY.handle(name, {}, ctx))
        assert result["success"] is False
        assert result["tool_access"] == "web"
    assert not mcp_server[0]


def test_unknown_provider_never_falls_back(context, mcp_server):
    ctx = replace(context, config=replace(context.config, tools=ToolAccessConfig(search_provider="custom")))
    with pytest.raises(ValueError, match="Unknown search provider"):
        DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, ctx)
    assert not mcp_server[0]


@pytest.mark.parametrize("mode", ["tool_error", "http_error", "invalid_payload", "invalid_url"])
def test_errors_reach_worker_without_fallback(context, mcp_server, monkeypatch, mode):
    requests, state, _ = mcp_server
    monkeypatch.setattr(web, "web_search", lambda *a, **kw: pytest.fail("unexpected DuckDuckGo fallback"))
    if mode == "tool_error":
        state["error"] = "Service quota exceeded"
    elif mode == "http_error":
        state["http_error"] = 503
    elif mode == "invalid_payload":
        state["search"] = {"unexpected": []}
    else:
        state["search"] = {"results": [{"url": "javascript:alert(1)"}]}
    result = run_one_step(
        context.job_id, config=context.config, db=context.db,
        llm=ScriptedLLM([LLMResponse(tool_calls=[ToolCall(name="web_search", arguments={"query": "source evidence"})])]),
    )
    assert result.status == "failed"
    assert result.result["success"] is False
    assert result.result["error"]
    assert requests


def test_extraction_limits_urls_and_preserves_errors(context, mcp_server):
    requests, state, _ = mcp_server
    state["fetch"] = {
        "results": [{"url": "https://example.com/source", "excerpts": ["one", "two"]}],
        "errors": [{"url": "https://example.com/missing", "message": "Not found"}],
    }
    urls = [f"https://example.com/{i}" for i in range(7)]
    result = json.loads(DEFAULT_REGISTRY.handle("web_extract", {"urls": urls}, context))
    assert result["pages"][0]["text"] == "one\n\ntwo"
    assert result["pages"][1] == {"url": "https://example.com/missing", "error": "Not found"}
    call = next(json.loads(r.content) for r in requests if json.loads(r.content)["method"] == "tools/call")
    assert call["params"]["arguments"]["urls"] == urls[:5]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com", "https://["])
def test_invalid_extraction_urls_do_not_connect(context, mcp_server, url):
    result = json.loads(DEFAULT_REGISTRY.handle("web_extract", {"urls": [url]}, context))
    assert result["success"] is False
    assert not mcp_server[0]


def test_mcp_timeout_closes_client(context, mcp_server, monkeypatch):
    _, state, clients = mcp_server
    state.update(wait=True, started=asyncio.Event())
    monkeypatch.setattr(parallel_web, "REQUEST_TIMEOUT_SECONDS", 0.05)
    result = json.loads(DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, context))
    assert result["success"] is False
    assert "timed out" in result["error"]
    assert all(client.is_closed for client in clients)


def test_mcp_cancellation_propagates_and_closes_client(mcp_server):
    _, state, clients = mcp_server

    async def cancel_call():
        state.update(wait=True, started=asyncio.Event())
        task = asyncio.create_task(parallel_web._call_tool("web_search", {"objective": "evidence", "search_queries": ["evidence"]}))
        await asyncio.wait_for(state["started"].wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_call())
    assert all(client.is_closed for client in clients)


def test_operator_interrupt_is_not_converted_to_service_error(context, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(parallel_web, "_request", interrupt)
    with pytest.raises(KeyboardInterrupt):
        DEFAULT_REGISTRY.handle("web_search", {"query": "source evidence"}, context)

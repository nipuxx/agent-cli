"""Optional anonymous Parallel Search MCP backend for the existing web tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
from builtins import BaseExceptionGroup
from typing import Any
from urllib.parse import urlsplit

from nipux_cli import __version__
from nipux_cli.source_quality import anti_bot_reason


MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = f"nipux/{__version__} (+https://github.com/nipuxx/agent-cli)"
REQUEST_TIMEOUT_SECONDS = 20


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import Implementation
    except ImportError as exc:
        raise RuntimeError("Parallel requires the optional dependency: install nipux[parallel]") from exc

    # Bound the entire handshake/discovery/call, including cleanup, rather than
    # allowing every individual request a separate full timeout.
    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(MCP_URL, http_client=client) as (read, write, _):
                async with ClientSession(
                    read, write, client_info=Implementation(name="nipux", version=__version__),
                ) as session:
                    await session.initialize()
                    await session.list_tools()
                    result = await session.call_tool(name, arguments)
                    text = "\n".join(block.text for block in result.content if block.type == "text")
                    if result.isError:
                        raise ValueError(text or "Parallel MCP tool failed")
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(text)
                    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                        raise ValueError("Parallel MCP returned an invalid results payload")
                    return data


def _error_text(exc: Exception) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_error_text(item) for item in exc.exceptions)
    if isinstance(exc, TimeoutError):
        return f"Parallel MCP request timed out after {REQUEST_TIMEOUT_SECONDS} seconds"
    return str(exc) or type(exc).__name__


def _request(name: str, arguments: dict[str, Any], session_id: str | None) -> dict[str, Any]:
    if session_id:
        # Correlate calls for a job without sending its title or raw identifier.
        arguments["session_id"] = hashlib.sha256(session_id.encode()).hexdigest()
    return asyncio.run(_call_tool(name, arguments))


def _http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def web_search(query: str, *, limit: int = 5, session_id: str | None = None) -> dict[str, Any]:
    try:
        data = _request("web_search", {"objective": query, "search_queries": [query]}, session_id)
        results = []
        for item in data["results"]:
            if not isinstance(item, dict) or not _http_url(item.get("url")):
                raise ValueError("Parallel MCP returned an invalid search result URL")
            results.append({
                "title": str(item.get("title") or item["url"]),
                "url": item["url"],
                "excerpts": item.get("excerpts") or [],
            })
        return {"success": True, "query": query, "results": results[:max(1, min(limit, 20))]}
    except Exception as exc:
        return {"success": False, "query": query, "results": [], "error": _error_text(exc)}


def web_extract(
    urls: list[str], *, limit_chars: int = 12_000, session_id: str | None = None,
) -> dict[str, Any]:
    urls = urls[:5]
    if not urls:
        return {"success": True, "pages": []}
    if not all(_http_url(url) for url in urls):
        return {"success": False, "pages": [], "error": "Parallel extraction requires HTTP or HTTPS URLs"}
    try:
        data = _request("web_fetch", {"urls": urls, "full_content": True}, session_id)
        pages = []
        for item in data["results"]:
            if not isinstance(item, dict) or not _http_url(item.get("url")):
                raise ValueError("Parallel MCP returned an invalid page URL")
            text = item.get("full_content")
            if text is None:
                text = "\n\n".join(item.get("excerpts") or [])
            if not isinstance(text, str):
                raise ValueError("Parallel MCP returned invalid page content")
            page = {
                "url": item["url"], "content_type": "text/markdown",
                "text": text[:limit_chars], "truncated": len(text) > limit_chars,
            }
            reason = anti_bot_reason(item["url"], text[:2000])
            if reason:
                page["source_warning"] = reason
                page["warnings"] = [{
                    "type": "anti_bot", "message": reason,
                    "guidance": "This page may require normal human browser verification. Do not bypass protections.",
                }]
            pages.append(page)
        for error in data.get("errors") or []:
            pages.append({"url": error.get("url", ""), "error": str(error.get("message") or error.get("error") or error)})
        if not pages:
            raise ValueError("Parallel MCP returned no pages")
        return {"success": True, "pages": pages}
    except Exception as exc:
        return {"success": False, "pages": [], "error": _error_text(exc)}

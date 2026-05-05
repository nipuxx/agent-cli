"""Small web search/extract helpers without external web tool dependencies."""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from nipux_cli.source_quality import anti_bot_reason


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs):
        del attrs
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self.skip_depth:
            text = data.strip()
            if text:
                self.parts.append(text)

    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s+", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return html.unescape(raw).strip()


def _request(url: str, *, timeout: int = 20, max_bytes: int = 2_000_000) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; nipux/0.1; +https://github.com/nipuxx/agent-cli)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"response exceeded {max_bytes} bytes")
    charset = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    return body.decode(charset, errors="replace"), content_type


def _strip_html(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    return parser.text()


def _duckduckgo_link(raw: str) -> str:
    parsed = urllib.parse.urlparse(html.unescape(raw))
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return query["uddg"][0]
    return html.unescape(raw)


def _ddg_search(query: str, *, limit: int = 5) -> dict[str, Any]:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    markup, _ = _request(url)
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.I | re.S,
    )
    results = []
    for match in pattern.finditer(markup):
        title = re.sub(r"<[^>]+>", "", match.group("title"))
        results.append({"title": html.unescape(title).strip(), "url": _duckduckgo_link(match.group("href"))})
        if len(results) >= limit:
            break
    return {"success": True, "query": query, "results": results}


def _ddg_extract(urls: list[str], *, limit_chars: int = 12_000) -> dict[str, Any]:
    pages = []
    for url in urls[:5]:
        try:
            body, content_type = _request(url)
            text = body if "text/plain" in content_type else _strip_html(body)
            reason = anti_bot_reason(url, text[:2000])
            page = {
                "url": url,
                "content_type": content_type,
                "text": text[:limit_chars],
                "truncated": len(text) > limit_chars,
            }
            if reason:
                page["source_warning"] = reason
                page["warnings"] = [{
                    "type": "anti_bot",
                    "message": reason,
                    "guidance": "This page may require normal human browser verification. Do not bypass protections.",
                }]
            pages.append(page)
        except Exception as exc:
            pages.append({"url": url, "error": str(exc)})
    return {"success": True, "pages": pages}


def _tavily_search(query: str, *, limit: int = 5) -> dict[str, Any]:
    from tavily import TavilyClient

    client = TavilyClient()
    response = client.search(query=query, max_results=limit)
    results = [
        {"title": r.get("title", ""), "url": r.get("url", "")}
        for r in response.get("results", [])
    ]
    return {"success": True, "query": query, "results": results}


def _tavily_extract(urls: list[str], *, limit_chars: int = 12_000) -> dict[str, Any]:
    from tavily import TavilyClient

    client = TavilyClient()
    response = client.extract(urls=urls[:5])
    pages = []
    for r in response.get("results", []):
        text = r.get("raw_content") or r.get("text") or ""
        pages.append({
            "url": r.get("url", ""),
            "text": text[:limit_chars],
            "truncated": len(text) > limit_chars,
        })
    for f in response.get("failed_results", []):
        pages.append({"url": f.get("url", ""), "error": f.get("error", "extraction failed")})
    return {"success": True, "pages": pages}


def web_search(query: str, *, limit: int = 5, search_provider: str = "duckduckgo") -> dict[str, Any]:
    if search_provider == "tavily":
        return _tavily_search(query, limit=limit)
    return _ddg_search(query, limit=limit)


def web_extract(urls: list[str], *, limit_chars: int = 12_000, search_provider: str = "duckduckgo") -> dict[str, Any]:
    if search_provider == "tavily":
        return _tavily_extract(urls, limit_chars=limit_chars)
    return _ddg_extract(urls, limit_chars=limit_chars)

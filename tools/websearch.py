"""Portable web search (standard library only).

Uses DuckDuckGo's HTML endpoint — no API key, no dependencies. This is a
best-effort research tool: for production use, swap in a proper search API
(Brave, Tavily, Exa) behind the same tool interface.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from .registry import Tool

_MAX_RESULTS = 8
_TIMEOUT = 20


def _strip(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _search_ddg(query: str, max_results: int = _MAX_RESULTS) -> str:
    params = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            page = resp.read(500_000).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - reported to the model
        return f"ERROR: search failed ({type(exc).__name__}: {exc})"

    results = []
    for m in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S
    ):
        link, title = m.group(1), _strip(m.group(2))
        # DuckDuckGo wraps outbound links in a redirect; unwrap it.
        parsed = urllib.parse.urlparse(link)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            link = qs["uddg"][0]
        if title and link.startswith("http"):
            results.append(f"- {title}\n  {link}")
        if len(results) >= max_results:
            break

    # Fallback: looser pattern if the markup changed.
    if not results:
        for m in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]{10,120})</a>', page):
            link, title = m.group(1), _strip(m.group(2))
            if "duckduckgo.com" in link:
                continue
            results.append(f"- {title}\n  {link}")
            if len(results) >= max_results:
                break

    if not results:
        return "(no results — the search page may have changed format)"
    return f"Results for {query!r}:\n" + "\n".join(results)


def web_search_tool() -> Tool:
    return Tool(
        name="web_search",
        description=(
            "Search the web and return titles + URLs of top results. "
            "Use for research, then web_fetch to read promising pages. "
            "No API key needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {"type": "string", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
        func=lambda query, max_results=8: _search_ddg(query, int(max_results)),
    )

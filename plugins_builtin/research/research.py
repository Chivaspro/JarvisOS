"""Built-in research plugin — real sources only.

It is a thin, honest façade over the research backends the app already has:

* **Agent Reach** (``capabilities.agent_reach``) for structured, multi-channel
  results including YouTube, when its environment is installed;
* the built-in **search** (``actions.web_search``) as the fallback, which is
  itself Gemini-grounded with a DuckDuckGo fallback.

Every line in the answer is a record a backend actually returned. When YouTube
is involved the answer says plainly that the media belongs to its owner — an
integration must not imply reuse rights it cannot grant.
"""

from __future__ import annotations

PLUGIN = {
    "name": "research",
    "description": "Research a topic across the web, news and YouTube with real sources.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING", "description": "What to research."},
            "platform": {"type": "STRING", "description": "web (default), news or youtube."},
            "max_results": {"type": "INTEGER", "description": "How many sources to return."},
        },
        "required": ["query"],
    },
}

_LICENSING = ("\n\nNote: linked videos and articles belong to their owners. "
              "Treat the links as references; reusing the media itself needs a "
              "licence from the rights holder.")

_PLATFORMS = ("web", "news", "youtube")


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _agent_reach(query: str, platform: str, max_results: int) -> dict | None:
    try:
        from capabilities import agent_reach
    except Exception:
        return None
    try:
        if not agent_reach.available():
            return None
    except Exception:
        return None
    try:
        return agent_reach.research(query=query, platform=platform,
                                    max_results=max_results)
    except Exception:
        return None


def _render_findings(query: str, platform: str, findings: list) -> str:
    lines = [f"Research results for {query!r} ({platform}):", ""]
    for index, finding in enumerate(findings[:25], 1):
        if not isinstance(finding, dict):
            continue
        title = str(finding.get("title") or finding.get("url") or f"Result {index}")
        url = str(finding.get("url") or "")
        snippet = str(finding.get("snippet") or "")
        source = str(finding.get("source") or finding.get("backend") or "")
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet[:300]}")
        if source:
            lines.append(f"   [source: {source}]")
    lines.append("")
    lines.append("Answer only from these records, and say which source each fact came from.")
    return "\n".join(lines)


def _search_fallback(query: str, platform: str) -> str:
    try:
        from actions.web_search import web_search
    except Exception as exc:
        return (f"Research is unavailable: no research backend is installed "
                f"({exc}). No results were produced.")
    mode = "news" if platform == "news" else "search"
    try:
        text = web_search({"query": query, "mode": mode})
    except Exception as exc:
        return (f"Research failed: the search backend raised {exc}. "
                f"No results were retrieved.")
    if not text or "Search failed" in str(text):
        return f"Research returned nothing usable for {query!r}: {text}"
    return str(text)


def run(parameters, settings=None, limits=None, logger=None) -> str:
    params = dict(parameters or {})
    settings = dict(settings or {})
    limits = dict(limits or {})
    query = str(params.get("query") or "").strip()
    if not query:
        return "Research needs a query."

    platform = str(params.get("platform") or "").strip().lower()
    if platform not in _PLATFORMS:
        platform = "web"

    default_n = _clamp(settings.get("max_results"), 1, 25,
                       _clamp(limits.get("max_results"), 1, 25, 6))
    max_results = _clamp(params.get("max_results"), 1, 25, default_n)
    include_youtube = bool(settings.get("include_youtube", True))

    sections: list[str] = []
    used_youtube = False

    # 1 · Structured multi-channel research (includes YouTube when available).
    reach_platform = platform
    if reach_platform == "web" and include_youtube:
        reach_platform = ""          # let Agent Reach use its channel set
    reach = _agent_reach(query, reach_platform, max_results)
    if reach and reach.get("ok"):
        findings = reach.get("findings") or []
        if findings:
            sections.append(_render_findings(query, reach_platform or "web", findings))
            used_youtube = any(
                str(f.get("source") or f.get("backend") or "").lower().find("youtube") >= 0
                for f in findings if isinstance(f, dict)
            )
        else:
            sections.append(str(reach.get("output") or "").strip())
    elif platform == "youtube":
        # A YouTube-only request with Agent Reach down: do NOT pretend. The
        # built-in search cannot list videos, so say what is actually available.
        reason = str((reach or {}).get("reason") or "Agent Reach is unavailable")
        fallback = _search_fallback(f"{query} site:youtube.com", "web")
        sections.append(
            f"YouTube search needs Agent Reach, which reported: {reason}.\n"
            f"The web search backend returned this instead:\n\n{fallback}")
        return "\n".join(sections)

    # 2 · The web/news answer itself.
    if platform in ("web", "news") or not sections:
        sections.append(_search_fallback(query, platform))

    answer = "\n\n".join(section for section in sections if section)
    answer = f"{answer}\n\n(Retrieved by J.A.R.V.I.S. research.)"
    if (used_youtube or platform == "youtube") and settings.get("licensing_note", True):
        answer += _LICENSING
    return answer

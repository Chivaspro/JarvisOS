"""Built-in SEO analyzer — measures the page it actually fetched.

There is no scoring model and no invented "SEO score": the report lists the
facts parsed out of the real HTML (title, meta description, headings, images,
links, structured data, byte size, time-to-fetch) and then points at the
concrete problems in those facts. If the page cannot be fetched, that failure
is the answer.
"""

from __future__ import annotations

import html.parser
import re
import time
import urllib.parse
import urllib.request

PLUGIN = {
    "name": "seo_analyzer",
    "description": "Analyse the on-page SEO of a real URL.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {"type": "STRING", "description": "Page URL to analyse."},
        },
        "required": ["url"],
    },
}

_TAG_RE = re.compile(r"<[^>]+>")


class _Collector(html.parser.HTMLParser):
    """A small, dependency-free HTML reader for the fields SEO cares about."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.metas: list[dict] = []
        self.links: list[dict] = []
        self.images: list[dict] = []
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self.canonical = ""
        self.ldjson = 0
        self._text_parts: list[str] = []
        self._in_title = False
        self._heading: str = ""
        self._heading_buf: list[str] = []
        self._in_script = False
        self._script_type = ""

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.metas.append(attrs)
        elif tag == "link":
            rel = (attrs.get("rel") or "").lower()
            if "canonical" in rel and attrs.get("href"):
                self.canonical = attrs["href"].strip()
        elif tag == "a":
            self.links.append({"href": attrs.get("href", "").strip(),
                               "text": "", "rel": (attrs.get("rel") or "").lower()})
        elif tag == "img":
            self.images.append({"src": attrs.get("src", "").strip(),
                                "alt": attrs.get("alt", ""),
                                "has_alt": "alt" in attrs})
        elif tag in self.headings:
            self._heading = tag
            self._heading_buf = []
        elif tag == "script":
            self._in_script = True
            self._script_type = (attrs.get("type") or "").lower()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_script = False
            if "ld+json" in self._script_type:
                self.ldjson += 1
        elif tag in self.headings and self._heading == tag:
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self.headings[tag].append(text)
            self._heading = ""
            self._heading_buf = []

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._heading:
            self._heading_buf.append(data)
        if not self._in_script and data.strip():
            self._text_parts.append(data)

    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def word_count(self) -> int:
        return len(" ".join(self._text_parts).split())


def _fetch(url: str, user_agent: str, timeout: int):
    """Return (final_url, body_text, status, length, elapsed_ms, error)."""
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent or "Mozilla/5.0 (compatible; JARVIS-SEO/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en",
    })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4_000_000)
            status = getattr(response, "status", 200)
            final_url = response.geturl() or url
            charset = response.headers.get_content_charset() or "utf-8"
    except Exception as exc:
        return url, "", 0, 0, (time.monotonic() - started) * 1000.0, str(exc)
    elapsed = (time.monotonic() - started) * 1000.0
    try:
        body = raw.decode(charset, errors="replace")
    except Exception:
        body = raw.decode("utf-8", errors="replace")
    return final_url, body, int(status), len(raw), elapsed, ""


def _meta_content(collector: _Collector, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for meta in collector.metas:
        key = (meta.get("name") or meta.get("property") or "").lower()
        if key in wanted:
            return " ".join((meta.get("content") or "").split())
    return ""


def _line(label: str, value, flag: str = "") -> str:
    marker = f"  {flag}" if flag else ""
    return f"• {label}: {value}{marker}"


def run(parameters, settings=None, limits=None, logger=None) -> str:
    params = dict(parameters or {})
    settings = dict(settings or {})
    url = str(params.get("url") or "").strip()
    if not url:
        return "SEO analysis needs a URL."
    if "://" not in url:
        url = "https://" + url

    user_agent = str(settings.get("user_agent") or "").strip()
    timeout = int(settings.get("timeout_seconds") or 20)
    max_links = int(settings.get("max_links") or 10)

    final_url, body, status, length, elapsed, error = _fetch(url, user_agent, timeout)
    if error:
        return (f"Could not fetch {url}: {error}. No analysis was possible, so "
                f"no SEO report was produced.")
    if status >= 400:
        return (f"{url} returned HTTP {status}. The page could not be analysed.")

    collector = _Collector()
    try:
        collector.feed(body)
    except Exception as exc:
        return f"The page was fetched but could not be parsed: {exc}"

    title = collector.title()
    description = _meta_content(collector, "description")
    robots = _meta_content(collector, "robots")
    viewport = _meta_content(collector, "viewport")
    og_title = _meta_content(collector, "og:title")
    og_desc = _meta_content(collector, "og:description")
    words = collector.word_count()

    base = urllib.parse.urlsplit(final_url)
    internal = external = 0
    for link in collector.links:
        href = link["href"]
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(final_url, href)
        host = urllib.parse.urlsplit(absolute).netloc.lower()
        if host == base.netloc.lower():
            internal += 1
        else:
            external += 1
    missing_alt = sum(1 for image in collector.images if not image["has_alt"])

    issues: list[str] = []
    if not title:
        issues.append("The page has no <title>.")
    elif not (30 <= len(title) <= 60):
        issues.append(f"Title is {len(title)} characters; 30–60 reads best in results.")
    if not description:
        issues.append("No meta description — search engines write their own.")
    elif not (50 <= len(description) <= 160):
        issues.append(f"Meta description is {len(description)} characters; 50–160 fits.")
    if len(collector.headings["h1"]) == 0:
        issues.append("No H1 heading.")
    elif len(collector.headings["h1"]) > 1:
        issues.append(f"{len(collector.headings['h1'])} H1 headings; one is the convention.")
    if missing_alt:
        issues.append(f"{missing_alt} of {len(collector.images)} images have no alt text.")
    if words < 300:
        issues.append(f"Only {words} words of visible text.")
    if not collector.canonical:
        issues.append("No canonical link.")
    if not viewport:
        issues.append("No viewport meta tag (mobile rendering).")
    if "noindex" in robots.lower():
        issues.append("meta robots says noindex — this page is asking not to be indexed.")

    lines = [f"SEO report for {final_url}", ""]
    lines.append(f"Fetched {length:,} bytes in {elapsed:.0f} ms (HTTP {status}).")
    if final_url.rstrip("/") != url.rstrip("/"):
        lines.append(f"Redirected from {url}.")
    lines.append("")
    lines.append("CONTENT")
    lines.append(_line("Title", f"{title!r} ({len(title)} chars)" if title else "missing"))
    lines.append(_line("Meta description",
                       f"{description!r} ({len(description)} chars)" if description else "missing"))
    lines.append(_line("Canonical", collector.canonical or "missing"))
    lines.append(_line("Robots", robots or "(none)"))
    lines.append(_line("Viewport", viewport or "missing"))
    lines.append(_line("Words", words))
    lines.append("")
    lines.append("HEADINGS")
    lines.append(_line("H1", f"{len(collector.headings['h1'])} — {collector.headings['h1'][:3]}"))
    lines.append(_line("H2", len(collector.headings["h2"])))
    lines.append(_line("H3", len(collector.headings["h3"])))
    lines.append("")
    lines.append("MEDIA & LINKS")
    lines.append(_line("Images", f"{len(collector.images)} ({missing_alt} without alt)"))
    lines.append(_line("Links", f"{internal} internal, {external} external"))
    lines.append(_line("Structured data", f"{collector.ldjson} JSON-LD block(s)"))
    lines.append("")
    lines.append("SOCIAL")
    lines.append(_line("og:title", og_title or "missing"))
    lines.append(_line("og:description", og_desc or "missing"))

    if max_links > 0:
        sample = [l["href"] for l in collector.links
                  if l["href"] and not l["href"].startswith("#")][:max_links]
        if sample:
            lines.append("")
            lines.append("SAMPLE LINKS")
            lines.extend(f"• {href}" for href in sample)

    lines.append("")
    advice = {
        "no <title>": "Write a 30–60 character <title> that names the page and the brand.",
        "title is": "Trim or expand the title into the 30–60 character window.",
        "no meta description": "Add a 50–160 character meta description that summarises the page.",
        "meta description is": "Rewrite the meta description into the 50–160 character window.",
        "no h1": "Add a single descriptive H1 near the top of the content.",
        "h1 headings": "Keep one H1 and demote the rest to H2/H3.",
        "without alt": "Give every meaningful image an alt attribute; leave decorative ones alt=\"\".",
        "words of visible text": "Add substantive prose — thin pages rarely rank.",
        "no canonical": "Add <link rel=\"canonical\"> pointing at the preferred URL.",
        "no viewport": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">.",
        "noindex": "Remove the noindex directive if this page should appear in search.",
    }

    def _recommend(issue: str) -> str:
        low = issue.lower()
        for needle, fix in advice.items():
            if needle in low:
                return fix
        return ""

    if issues:
        lines.append("FOUND")
        lines.extend(f"• {issue}" for issue in issues)
        if settings.get("recommendations", True):
            fixes = [_recommend(issue) for issue in issues]
            fixes = [fix for fix in fixes if fix]
            if fixes:
                lines.append("")
                lines.append("RECOMMENDATIONS")
                lines.extend(f"• {fix}" for fix in fixes)
    else:
        lines.append("FOUND: no issues in the checks this tool performs.")
    return "\n".join(lines)

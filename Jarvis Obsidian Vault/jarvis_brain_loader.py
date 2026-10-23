#!/usr/bin/env python3
"""Jarvis Brain Loader v2 (zero dependencies, Python 3.9+).

Commands
  prompt                         print the system prompt
  context "question" [-b 3000]   memory block to inject into a prompt (ranked, budgeted)
  search "query" [-k 5]          ranked recall (TF-IDF x confidence x recency x category)
  add "Title" "text" [--category C] [--confidence 0.8]
  reinforce "Title"              confidence +0.05 (max 0.99)
  supersede "Old" "New"          mark Old as replaced by New
  forget "Title"                 delete a note completely
  links "Title"                  outgoing links and backlinks
  health                         broken links, orphans, secrets, low confidence, missing fields
  consolidate                    list likely duplicate notes
  index                          rebuild Memory/System/Memory Index.md
  stats                          vault overview

Import:  from jarvis_brain_loader import system_prompt, context, search, add_memory
"""
import argparse
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT = Path(__file__).resolve().parent
MEMORY = VAULT / "Memory"
GMT8 = timezone(timedelta(hours=8))
FOLDERS = ["core", "personality", "decisions", "coding", "conversations", "projects",
           "preferences", "goals", "people", "knowledge", "system", "improvement", "tools", "guardian"]
NO_DECAY = {"core", "preferences", "personality", "system"}
SECRET = re.compile(
    r"((api[_ -]?key|password|passwd|secret|token)\s*(is|=|:)\s*\S+|sk-[A-Za-z0-9]{10,}|\b\d{13,19}\b)", re.I)
LINK = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")
BOOSTS = {
    "coding": ({"code", "python", "bug", "function", "script", "api", "error", "debug", "class", "test",
                "async", "database", "git", "program", "build"}, {"coding": 1.3, "knowledge": 1.1}),
    "personal": ({"tone", "voice", "personality", "funny", "feel", "style", "sarcastic", "name", "who"},
                 {"personality": 1.3, "preferences": 1.3, "core": 1.2}),
    "planning": ({"goal", "plan", "decide", "decision", "priority", "risk", "choose", "should", "strategy"},
                 {"decisions": 1.3, "goals": 1.3, "projects": 1.1}),
}


# ---------- parsing ----------
def parse(path: Path):
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
        body = m.group(2)
    return meta, body.strip()


STOP = set("a an the is are was were be been am do does did how what when where why who which i me my mine we "
           "you your it its of to in on at for with and or but if so up as by from this that these those can "
           "could should would will shall may might not no yes please jarvis sir tell about into than then".split())


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


SYN = {
    "name": ["identity", "nickname", "address", "chivas"], "who": ["identity", "profile"],
    "speed": ["performance", "optimization", "fast", "profiling"], "slow": ["performance", "optimization", "profiling"],
    "fast": ["performance", "speed"], "optimize": ["performance", "optimization"],
    "talk": ["voice", "tone", "style", "response", "language"], "speak": ["voice", "language", "english"],
    "tone": ["voice", "personality"], "joke": ["humor", "sarcasm"], "funny": ["humor", "sarcasm"],
    "procrastinate": ["focus", "coach", "excuse", "deadline", "productivity"],
    "lazy": ["focus", "coach", "productivity"], "motivation": ["coach", "focus", "goals"],
    "meaning": ["embedding", "semantic", "rag"], "semantic": ["embedding", "rag"],
    "remember": ["memory", "retrieval", "storage"], "memory": ["architecture", "retrieval", "consolidation"],
    "forget": ["forgetting", "delete", "privacy"], "decide": ["decision", "framework", "tradeoff"],
    "choose": ["decision", "tradeoff", "option"], "pick": ["decision", "tradeoff", "option"],
    "option": ["tradeoff", "decision"], "risk": ["risk", "premortem", "safety"],
    "test": ["testing", "pytest"], "bug": ["debugging", "error", "fix"], "error": ["debugging", "logging"],
    "crash": ["debugging", "error"], "secure": ["security", "checklist"], "hack": ["security"],
    "money": ["billionaire", "business"], "rich": ["billionaire", "business"],
    "learn": ["learning", "strategy", "study"], "study": ["learning", "strategy"],
    "improve": ["improvement", "backlog", "loop"], "upgrade": ["improvement", "roadmap"],
    "language": ["language", "guide", "english"], "database": ["database", "sql", "storage"],
    "async": ["concurrency", "asyncio"], "reverse": ["rea", "reverse-engineering"], "working": ["troubleshooting", "error"], "broken": ["troubleshooting", "error"], "fail": ["troubleshooting", "error"], "install": ["setup"], "tutorial": ["guide", "learning"], "legal": ["safety", "legal"], "hack": ["guardian", "security"], "hacker": ["guardian", "security"], "virus": ["malware", "guardian"], "malware": ["guardian", "removal"], "remove": ["guardian", "removal"], "security": ["guardian", "hardening"], "protect": ["guardian", "hardening"], "hacked": ["guardian", "incident"], "binary": ["rea", "target"], "decompile": ["rea", "reverse-engineering"], "electron": ["rea", "target"], "mcp": ["rea", "overview"], "analyze": ["rea", "workflow"], "thread": ["concurrency"], "git": ["git", "workflow"],
}
def _root(w: str) -> str:
    for suf in ("ing", "ed", "es", "e", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


_SYN = {_root(k): v for k, v in SYN.items()}


def expand(words):
    out = list(words)
    for w in words:
        out.extend(_SYN.get(_root(w), []))
    return out


def tokens(s: str):
    return [_stem(w) for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in STOP]


def load_all():
    notes = []
    for p in sorted(MEMORY.rglob("*.md")):
        meta, body = parse(p)
        try:
            conf = float(meta.get("confidence", 0.5))
        except ValueError:
            conf = 0.5
        try:
            updated = datetime.strptime(meta.get("updated", meta.get("created", "")), "%Y-%m-%d %H:%M")
        except ValueError:
            updated = None
        notes.append({"path": p, "meta": meta, "title": meta.get("title", p.stem),
                      "category": meta.get("category", "").lower(), "tags": meta.get("tags", ""),
                      "conf": conf, "updated": updated, "body": body})
    return notes


def find(title: str, notes=None):
    notes = notes or load_all()
    t = title.strip().lower()
    for n in notes:
        if n["title"].lower() == t or n["path"].stem.lower() == t:
            return n
    return None


# ---------- retrieval ----------
def _vec(n):
    c = Counter()
    for w in tokens(n["title"]):
        c[w] += 6
    for w in tokens(n["tags"]):
        c[w] += 3
    for w in tokens(n["body"]):
        c[w] += 1
    return c


def search(query: str, k: int = 5, notes=None):
    notes = notes or load_all()
    base = tokens(query)
    qt = Counter(expand(base))
    for w in base:
        qt[w] += 1  # originals count double
    if not qt or not notes:
        return []
    vecs = [_vec(n) for n in notes]
    df = Counter(w for v in vecs for w in v)
    N = len(notes)
    idf = lambda w: math.log((N + 1) / (df.get(w, 0) + 1)) + 1
    qvec = {w: c * idf(w) for w, c in qt.items()}
    qnorm = math.sqrt(sum(x * x for x in qvec.values())) or 1
    qwords = set(qt)
    boosts = {}
    for _, (kw, cats) in BOOSTS.items():
        if qwords & kw:
            for cat, b in cats.items():
                boosts[cat] = max(boosts.get(cat, 1), b)
    now = datetime.now(GMT8).replace(tzinfo=None)
    out = []
    for n, v in zip(notes, vecs):
        dot = sum(qvec[w] * c * idf(w) for w, c in v.items() if w in qvec)
        if dot <= 0:
            continue
        norm = math.sqrt(sum((c * idf(w)) ** 2 for w, c in v.items())) or 1
        rel = dot / (qnorm * norm)
        if n["category"] in NO_DECAY or not n["updated"]:
            rec = 1.1
        else:
            age = max((now - n["updated"]).days, 0)
            rec = 1 + 0.2 * max(0.0, 1 - age / 90)
        score = rel * n["conf"] * rec * boosts.get(n["category"], 1.0)
        out.append((score, n))
    out.sort(key=lambda x: x[0], reverse=True)
    return [dict(n, score=s) for s, n in out[:k]]


def system_prompt() -> str:
    _, body = parse(MEMORY / "Core" / "System Prompt.md")
    body = re.sub(r"^# System Prompt\s*", "", body)
    return body.split("\n## Related")[0].strip()


def _snippet(body: str, limit: int = 600):
    body = re.sub(r"\n## Related.*", "", body, flags=re.S)
    body = re.sub(r"^# .*\n", "", body).strip()
    return body if len(body) <= limit else body[:limit].rsplit(" ", 1)[0] + " ..."


def context(query: str, budget: int = 3000, k: int = 6):
    notes = load_all()
    hits = search(query, k, notes)
    chosen, used, seen = [], 0, set()

    def add(n, tag=""):
        nonlocal used
        if n["title"] in seen:
            return
        block = f"### {n['title']} [{n['category']}, conf {n['conf']:.2f}]{tag}\n{_snippet(n['body'])}\n"
        if used + len(block) > budget:
            return
        chosen.append(block); used += len(block); seen.add(n["title"])

    for n in hits:
        add(n)
    for n in hits[:3]:  # one-hop link expansion
        for target in LINK.findall(n["body"]):
            t = find(target, notes)
            if t:
                add(t, " (linked)")
    return "\n".join(chosen)


# ---------- writing ----------
def _stamp():
    return datetime.now(GMT8).strftime("%Y-%m-%d %H:%M")


def _label(c):
    return "high" if c >= 0.8 else "medium" if c >= 0.5 else "low"


def _set_meta(path: Path, **kv):
    text = path.read_text(encoding="utf-8")
    for k, v in kv.items():
        if re.search(rf"^{k}:.*$", text, re.M):
            text = re.sub(rf"^{k}:.*$", f"{k}: {v}", text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8")


def add_memory(title, text, category="knowledge", confidence=0.8, source="conversation"):
    if SECRET.search(title + " " + text):
        raise ValueError("Refusing to store what looks like a secret (key, password or card/ID number).")
    category = category.lower() if category.lower() in FOLDERS else "knowledge"
    folder = MEMORY / category.capitalize()
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "", title).strip()[:80] or "note"
    path = folder / f"{safe}.md"
    now = _stamp()
    tags = ", ".join(sorted(set(tokens(title)))[:6])
    path.write_text(
        f'---\ntitle: "{title}"\ncategory: {category}\njarvis: true\nsource: {source}\n'
        f"confidence: {confidence:.2f}\nconfidence_label: {_label(confidence)}\ncreated: {now}\nupdated: {now}\n"
        f"tags: [{category}, jarvis-memory, {tags}]\n---\n\n# {title}\n\n{text}\n", encoding="utf-8")
    return path


def reinforce(title):
    n = find(title)
    if not n:
        raise KeyError(f"No note titled '{title}'")
    c = min(0.99, n["conf"] + 0.05)
    _set_meta(n["path"], confidence=f"{c:.2f}", confidence_label=_label(c), updated=_stamp())
    return c


def supersede(old, new):
    notes = load_all()
    o, n = find(old, notes), find(new, notes)
    if not o or not n:
        raise KeyError("Both notes must exist")
    c = min(o["conf"], 0.3)
    _set_meta(o["path"], confidence=f"{c:.2f}", confidence_label="low", updated=_stamp())
    with o["path"].open("a", encoding="utf-8") as f:
        f.write(f"\n> Superseded by [[{n['title']}]] on {_stamp()}\n")
    with n["path"].open("a", encoding="utf-8") as f:
        f.write(f"\n> Supersedes [[{o['title']}]]\n")


def forget(title):
    n = find(title)
    if not n:
        raise KeyError(f"No note titled '{title}'")
    n["path"].unlink()
    return n["path"]


# ---------- analysis ----------
def links_of(title):
    notes = load_all()
    n = find(title, notes)
    if not n:
        raise KeyError(f"No note titled '{title}'")
    out = sorted(set(LINK.findall(n["body"])))
    back = sorted(m["title"] for m in notes if m is not n and
                  any(t.strip().lower() in (n["title"].lower(), n["path"].stem.lower()) for t in LINK.findall(m["body"])))
    return out, back


def health():
    notes = load_all()
    names = {}
    for n in notes:
        names[n["title"].lower()] = n
        names[n["path"].stem.lower()] = n
    inbound = Counter()
    issues = {"missing fields": [], "broken links": [], "orphans": [], "low confidence": [],
              "possible secrets": [], "duplicate titles": []}
    seen_titles = Counter(n["title"].lower() for n in notes)
    for n in notes:
        for f in ("title", "category", "confidence", "created"):
            if f not in n["meta"]:
                issues["missing fields"].append(f"{n['title']} (no {f})")
        for t in LINK.findall(n["body"]):
            tgt = names.get(t.strip().lower())
            if tgt is None:
                issues["broken links"].append(f"{n['title']} -> [[{t}]]")
            elif tgt is not n:
                inbound[tgt["title"]] += 1
        if n["conf"] < 0.5:
            issues["low confidence"].append(f"{n['title']} ({n['conf']:.2f})")
        if SECRET.search(n["body"]):
            issues["possible secrets"].append(n["title"])
        if seen_titles[n["title"].lower()] > 1:
            issues["duplicate titles"].append(n["title"])
    for n in notes:
        if inbound[n["title"]] == 0 and n["category"] != "conversations":
            issues["orphans"].append(n["title"])
    return notes, issues


def consolidate():
    notes = load_all()
    sets = [(n, set(tokens(n["title"]))) for n in notes]
    pairs = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            a, b = sets[i][1], sets[j][1]
            if a and b and len(a & b) / len(a | b) >= 0.6:
                pairs.append((sets[i][0]["title"], sets[j][0]["title"]))
    return pairs


def rebuild_index():
    notes = load_all()
    groups = {}
    for n in notes:
        if n["title"] != "Memory Index":
            groups.setdefault(n["category"] or "other", []).append(n["title"])
    order = FOLDERS + [c for c in groups if c not in FOLDERS]
    lines = []
    for c in order:
        if c in groups:
            links = ", ".join(f"[[{t}]]" for t in sorted(groups[c]))
            lines.append(f"**{c.capitalize()}** ({len(groups[c])}): {links}\n")
    now = _stamp()
    path = MEMORY / "System" / "Memory Index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\ntitle: "Memory Index"\ncategory: system\njarvis: true\nsource: auto-index\nconfidence: 0.95\n'
        f"confidence_label: high\ncreated: {now}\nupdated: {now}\ntags: [index, map, memory]\n---\n\n"
        f"# Memory Index\n\nMap of the brain ({len(notes)} notes). Start with [[System Prompt]]. "
        f"Rebuild with `python jarvis_brain_loader.py index`.\n\n" + "\n".join(lines), encoding="utf-8")
    return len(notes)


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Jarvis brain loader v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prompt")
    c = sub.add_parser("context"); c.add_argument("query"); c.add_argument("-b", type=int, default=3000)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-k", type=int, default=5)
    a = sub.add_parser("add"); a.add_argument("title"); a.add_argument("text")
    a.add_argument("--category", default="knowledge"); a.add_argument("--confidence", type=float, default=0.8)
    r = sub.add_parser("reinforce"); r.add_argument("title")
    sp = sub.add_parser("supersede"); sp.add_argument("old"); sp.add_argument("new")
    f = sub.add_parser("forget"); f.add_argument("title")
    l = sub.add_parser("links"); l.add_argument("title")
    for name in ("health", "consolidate", "index", "stats"):
        sub.add_parser(name)
    args = ap.parse_args()

    try:
        if args.cmd == "prompt":
            print(system_prompt())
        elif args.cmd == "context":
            print(context(args.query, args.b))
        elif args.cmd == "search":
            for n in search(args.query, args.k):
                print(f"{n['score']:.3f}  [{n['category']}] {n['title']} (conf {n['conf']:.2f})")
        elif args.cmd == "add":
            print("Saved:", add_memory(args.title, args.text, args.category, args.confidence))
        elif args.cmd == "reinforce":
            print(f"Confidence now {reinforce(args.title):.2f}")
        elif args.cmd == "supersede":
            supersede(args.old, args.new); print("Done.")
        elif args.cmd == "forget":
            print("Deleted:", forget(args.title))
        elif args.cmd == "links":
            out, back = links_of(args.title)
            print("Links to:", ", ".join(out) or "none"); print("Linked from:", ", ".join(back) or "none")
        elif args.cmd == "health":
            notes, issues = health()
            print(f"{len(notes)} notes checked")
            bad = 0
            for k, v in issues.items():
                if v:
                    bad += len(v); print(f"\n{k} ({len(v)}):"); [print("  -", x) for x in v]
            print("\nAll clear." if not bad else f"\n{bad} issue(s) found.")
        elif args.cmd == "consolidate":
            p = consolidate()
            print("Possible duplicates:" if p else "No likely duplicates.")
            for x, y in p:
                print(f"  - {x}  <->  {y}")
        elif args.cmd == "index":
            print(f"Index rebuilt for {rebuild_index()} notes.")
        elif args.cmd == "stats":
            notes = load_all(); by = Counter(n["category"] or "uncategorized" for n in notes)
            avg = sum(n["conf"] for n in notes) / max(len(notes), 1)
            print(f"{len(notes)} memories, average confidence {avg:.2f}")
            for k, v in sorted(by.items()):
                print(f"  {k}: {v}")
    except (KeyError, ValueError) as e:
        print(e, file=sys.stderr); sys.exit(1)


if __name__ == "__main__":
    main()

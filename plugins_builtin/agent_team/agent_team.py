"""Built-in Agent Team — roles backed by real engines, never by a script.

Each role is a thin router to a subsystem the app already ships:

* **researcher** → ``capabilities.router.handle_web_research`` (live research);
* **analyst / planner** → the configured model via ``app.llm_client``;
* **code_reviewer** → reads a real file and asks the model to review it;
* **automation** → ``capabilities.sequence`` (gated desktop steps);
* **developer** → deliberately refuses. Writing or editing code from inside a
  plugin would bypass the coding approval gate, so the role points at the
  ``code_helper`` tool instead of silently doing unsafe work.

If a backend is missing or the model cannot be reached, the role returns that
fact. It never fabricates a result.
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN = {
    "name": "agent_team",
    "description": "Route a task to a specialised agent role.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "role": {"type": "STRING", "description": "Agent role."},
            "task": {"type": "STRING", "description": "What to do."},
            "context": {"type": "STRING", "description": "Material to use."},
            "path": {"type": "STRING", "description": "File to review."},
            "steps": {"type": "STRING", "description": "Steps for the automation role."},
        },
        "required": ["role", "task"],
    },
}

ROLES = ("researcher", "analyst", "planner", "code_reviewer", "automation",
         "developer")

_SYSTEM = {
    "analyst": ("You are the Analyst on a small agent team. Use only the material "
                "you are given plus well-established general knowledge. State "
                "uncertainty plainly and never invent facts, figures or sources."),
    "planner": ("You are the Planner on a small agent team. Produce a short, "
                "ordered plan whose steps each name a concrete action. Do not "
                "claim any step has been executed."),
    "code_reviewer": ("You are the Code Reviewer on a small agent team. Review "
                      "only the code you are given. List real problems with the "
                      "line or symbol they affect; say when you find none."),
}


def _model(settings: dict) -> str | None:
    value = str(settings.get("model") or "").strip()
    return value or None


def _llm(prompt: str, system: str, settings: dict, logger) -> str:
    try:
        from app.llm_client import call_llm_text
    except Exception as exc:
        return f"The reasoning model is unavailable: {exc}"
    try:
        text = call_llm_text(prompt, system=system, model=_model(settings))
    except Exception as exc:
        return f"The reasoning model call failed: {exc}"
    return str(text or "").strip() or "The model returned an empty answer."


def _researcher(task: str, context: str) -> str:
    try:
        from capabilities import router
    except Exception as exc:
        return f"Research is unavailable: {exc}"
    try:
        return router.handle_web_research({"query": task, "context": context})
    except Exception as exc:
        return f"Research failed: {exc}"


def _code_reviewer(params: dict, settings: dict, logger) -> str:
    raw = str(params.get("path") or "").strip().strip('"')
    excerpt = str(params.get("context") or "").strip()
    if raw:
        target = Path(raw)
        if not target.exists():
            return f"There is no file at {raw}, so nothing was reviewed."
        try:
            excerpt = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"The file could not be read: {exc}"
        header = f"Review the file {raw}.\n\n"
    else:
        if not excerpt:
            return "Code Reviewer needs a file path or code in 'context'."
        header = "Review the following code.\n\n"
    limit = int(settings.get("review_chars") or 12000)
    body = excerpt[:limit]
    if len(excerpt) > limit:
        body += f"\n… ({len(excerpt) - limit} more characters not shown)"
    return header + _llm(body, _SYSTEM["code_reviewer"], settings, logger)


def _automation(params: dict) -> str:
    steps = params.get("steps")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError:
            return "The automation role needs 'steps' as a JSON array."
    if not isinstance(steps, list) or not steps:
        return "The automation role needs a non-empty 'steps' array."
    try:
        from capabilities import sequence
    except Exception as exc:
        return f"The desktop-sequence engine is unavailable: {exc}"
    result = sequence.run({"steps": steps})
    return str(result.get("output") or "The sequence reported no output.")


def run(parameters, settings=None, limits=None, logger=None) -> str:
    params = dict(parameters or {})
    settings = dict(settings or {})
    role = str(params.get("role") or "").strip().lower().replace(" ", "_")
    task = str(params.get("task") or "").strip()
    context = str(params.get("context") or "").strip()

    if role not in ROLES:
        return (f"Unknown role '{role}'. Available roles: "
                f"{', '.join(ROLES)}.")
    if not task and role != "code_reviewer":
        return "An agent role needs a task."

    if role == "researcher":
        return _researcher(task, context)
    if role == "analyst":
        material = task if not context else f"{task}\n\nMaterial:\n{context}"
        return _llm(material, _SYSTEM["analyst"], settings, logger)
    if role == "planner":
        material = task if not context else f"{task}\n\nContext:\n{context}"
        return _llm(material, _SYSTEM["planner"], settings, logger)
    if role == "code_reviewer":
        return _code_reviewer(params, settings, logger)
    if role == "automation":
        return _automation(params)

    # developer
    return ("The Developer role does not write code from inside a plugin: that "
            "would bypass the coding approval gate. Ask me to make the change "
            "directly (the coding tool shows an approval card first), or use the "
            "Planner role to produce a plan.")

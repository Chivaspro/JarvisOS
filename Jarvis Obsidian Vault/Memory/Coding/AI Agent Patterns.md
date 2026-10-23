---
title: "AI Agent Patterns"
category: coding
jarvis: true
source: setup-interview
confidence: 0.95
confidence_label: high
created: 2026-10-16 09:00
updated: 2026-10-16 09:00
tags: [agents, tools, llm, python]
---

# AI Agent Patterns

A compact, working tool-registry pattern for Jarvis (Python):

```python
from typing import Callable, Dict

TOOLS: Dict[str, Callable] = {}

def tool(name: str):
    def register(fn: Callable) -> Callable:
        TOOLS[name] = fn
        return fn
    return register

@tool("time")
def current_time() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M (GMT+8)")

def run_tool(name: str, **kwargs):
    if name not in TOOLS:
        return f"Unknown tool: {name}"
    try:
        return TOOLS[name](**kwargs)
    except Exception as exc:
        return f"Tool '{name}' failed: {exc}"
```

**Agent loop in plain words:** read message, recall relevant memories, decide (answer or use a tool), act, observe the result, save anything worth remembering, reply.
**Rules:** tools return plain data, log every call, require confirmation for risky tools.

## Related
- [[Architecture Patterns]]
- [[Coding Brain]]
- [[Memory Rules]]

---
title: "REA With Jarvis"
category: tools
jarvis: true
source: brain-upgrade-v3
confidence: 0.85
confidence_label: high
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, jarvis, integration, python]
---

# REA With Jarvis

Two ways Jarvis can use REA:

**A. Through MCP (best):** run `npx rea-agents setup`, choose your agent, restart. The agent then calls REA tools itself and returns evidence.

**B. Through the terminal from Jarvis's Python code:** use `jarvis_rea.py` in this vault. It:
- calls the `rea` CLI (or `npx rea-agents@latest`) with **no shell**, a **timeout**, and an **audit log**
- **refuses to run unless authorization is confirmed** (`authorized=True`)
- returns parsed JSON

```python
from jarvis_rea import analyze_javascript_application

result = analyze_javascript_application("/absolute/path/to/my_app", authorized=True)
print(list(result)[:10])
```
CLI: `python jarvis_rea.py js /absolute/path/to/my_app --authorized` and `python jarvis_rea.py help`.

**Flow in Jarvis:** Sir asks, Jarvis checks [[REA Safety And Legal]], picks the target type ([[REA Smart Usage Playbook]]), runs, summarizes with evidence and limits, then saves a case file and memories.

## Related
- [[REA Setup]]
- [[REA Smart Usage Playbook]]
- [[AI Agent Patterns]]

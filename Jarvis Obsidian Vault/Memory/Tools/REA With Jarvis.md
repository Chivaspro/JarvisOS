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

Three ways Jarvis can use REA:

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

**C. Through the native `reverse_engineer` tool (the remake half):** REA itself only inspects — it never produces a runnable copy. Jarvis's own `reverse_engineer` tool is the missing half:
- `capture` — Playwright records a page you own (DOM, screenshot, computed styles, network traffic as HAR) into `data/workspaces/rea/<domain>/<run>/`
- `api_surface` — the API endpoints the captured traffic revealed
- `remake` — a clean, original project (index.html + assets + optional mock server) synthesised from the evidence and verified; a clean-room reproduction, never a byte-for-byte copy
- `reconstruct` — copy an app folder's own sources into a readable tree; when REA is connected its `recover_javascript_sources` adds compiled bundles
- Every run needs Sir's explicit authorization (`authorized=true`) and is appended to `data/workspaces/rea/audit.log`. Switch: Settings ▸ Permissions ▸ "Allow reverse engineering".

**Flow in Jarvis:** Sir asks, Jarvis checks [[REA Safety And Legal]], picks the target type ([[REA Smart Usage Playbook]]), runs, summarizes with evidence and limits, then saves a case file and memories. For "copy it for me" requests: authorize → `reverse_engineer` capture → `api_surface` → `remake` (or `reconstruct` for an app folder).

## Related
- [[REA Setup]]
- [[REA Smart Usage Playbook]]
- [[AI Agent Patterns]]

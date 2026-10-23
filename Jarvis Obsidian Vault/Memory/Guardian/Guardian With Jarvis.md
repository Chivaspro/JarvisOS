---
title: "Guardian With Jarvis"
category: guardian
jarvis: true
source: brain-upgrade-v4
confidence: 0.85
confidence_label: high
created: 2026-10-16 16:00
updated: 2026-10-16 16:00
tags: [guardian, python, jarvis, monitoring]
---

# Guardian With Jarvis

`jarvis_guardian.py` in this vault is a **defensive, read-only** system checker. It does not remove anything by itself — it reports, Sir decides, then runs the removal steps from [[Malware Removal]] manually or approves each action.

**What it checks (Windows, macOS, Linux via Python's standard library + `psutil`)**
- Running processes with no recognizable publisher / running from Temp or unusual paths.
- Listening network ports and which process owns them.
- Startup/auto-run entries.
- Recently modified files in sensitive folders (last 24h).
- Basic log tail for repeated failed logins (where available).

**Usage**
```
pip install psutil
python jarvis_guardian.py scan
python jarvis_guardian.py scan --json
```
It never deletes, kills processes, or changes settings — it only reports, with a findings list Jarvis can summarize and explain with evidence (same principle as REA: [[REA Smart Usage Playbook]]). Any actual removal is a **separate, confirmed** step Sir approves, following [[Malware Removal]].

This follows [[Guardian Rules Of Engagement]]: observe and report on **your own** machine, never act on someone else's.

## Related
- [[Guardian Overview]]
- [[Threat Detection]]
- [[AI Agent Patterns]]

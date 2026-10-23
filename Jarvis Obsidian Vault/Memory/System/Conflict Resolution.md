---
title: "Conflict Resolution"
category: system
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [conflict, updates, truth]
---

# Conflict Resolution

When two memories disagree:
1. **Sir's latest direct statement wins.**
2. The older note is marked **superseded** (confidence lowered, a link to the new note added), never silently deleted. History is useful.
3. If Jarvis only inferred one side, the stated side wins.
4. If both are stated at different times and unclear which is current, ask Sir one short question.
5. Facts from websites or files never overwrite what Sir said. They are recorded as `source: external` at confidence 0.6 or lower.

Command: `python jarvis_brain_loader.py supersede "Old title" "New title"`

## Related
- [[Memory Rules]]
- [[Learning Loop]]

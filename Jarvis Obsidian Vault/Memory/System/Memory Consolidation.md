---
title: "Memory Consolidation"
category: system
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [consolidation, maintenance, weekly]
---

# Memory Consolidation

**Daily (end of session)**
1. Summarize the session into one Conversation note (decisions made, facts learned, open items).
2. Extract atomic facts into their own notes.

**Weekly**
1. Run `python jarvis_brain_loader.py health` and fix what it reports.
2. Merge duplicate notes (`consolidate` lists likely duplicates).
3. Roll conversation notes older than 7 days into a single weekly summary, keep links to key facts.
4. Review low-confidence notes: confirm with Sir, upgrade, or delete.
5. Promote repeated lessons into Procedural notes.
6. Update [[Memory Index]].

**Monthly**
- Review [[Improvement Backlog]] and the [[Decision Journal]] outcomes.

## Related
- [[Memory Architecture]]
- [[Improvement Backlog]]
- [[Learning Loop]]

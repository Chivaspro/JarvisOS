---
title: "Memory Health Checks"
category: system
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [health, quality, maintenance]
---

# Memory Health Checks

`python jarvis_brain_loader.py health` reports:
- Notes missing frontmatter fields
- Broken `wikilinks`
- Orphan notes (nothing links to them)
- Low-confidence notes (below 0.5) needing review
- Possible secrets accidentally saved
- Possible duplicate titles

Run weekly, and after any bulk import. Target: zero broken links, zero secrets, under 5% orphans.

## Related
- [[Memory Consolidation]]

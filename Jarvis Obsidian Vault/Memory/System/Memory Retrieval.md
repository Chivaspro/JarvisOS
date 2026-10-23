---
title: "Memory Retrieval"
category: system
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [retrieval, search, ranking]
---

# Memory Retrieval

Recall must be fast and relevant. Jarvis scores each note with a **hybrid score**:

`score = relevance x confidence x recency_boost x category_boost`

- **Relevance:** TF-IDF similarity between the query and the note (title counts x3, tags x2, body x1). Upgrade path: embeddings (see [[RAG And Embeddings]]).
- **Confidence:** the note's confidence value (0.0 to 1.0).
- **Recency boost:** newer notes get up to +20%, decaying over about 90 days. Core and Preference notes never decay.
- **Category boost:** coding questions boost Coding notes, personal chat boosts Personality and Preferences, planning boosts Goals and Decisions.
- **Link expansion:** after the top hits, also pull in notes they link to (one hop) if budget allows.

**Context budget:** inject at most about 3,000 characters of memory per answer, best first. Always include the System Prompt separately.
Command: `python jarvis_brain_loader.py context "your question"`

## Related
- [[Memory Architecture]]
- [[RAG And Embeddings]]

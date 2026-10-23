---
title: "Architecture Patterns"
category: coding
jarvis: true
source: setup-interview
confidence: 0.95
confidence_label: high
created: 2026-10-16 09:00
updated: 2026-10-16 09:00
tags: [architecture, design, patterns]
---

# Architecture Patterns

Patterns to reach for when building bigger systems (including Jarvis itself):

- **Plugin / tool registry:** features register themselves, so adding one never touches core code.
- **Event bus:** modules communicate through events, not direct calls.
- **Layered memory:** short-term (current chat), long-term (vault notes), semantic (embeddings index).
- **Async I/O** for anything waiting on network, voice or disk.
- **Config over code:** settings in a file or `.env`, not scattered constants.
- **Separation:** UI, logic and data in separate modules so each can be replaced.
- **Fail soft:** if one component dies, Jarvis keeps running and reports it.
- **Idempotent actions:** running something twice should not break anything.

## Related
- [[Coding Brain]]
- [[AI Agent Patterns]]
- [[Jarvis Roadmap]]

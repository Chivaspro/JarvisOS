---
title: "Memory Architecture"
category: system
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [memory, architecture, layers]
---

# Memory Architecture

Jarvis's memory has five layers, like a human brain:

| Layer | What it holds | Where it lives | Lifespan |
|---|---|---|---|
| **Working** | The current conversation and task | Context window | Minutes |
| **Episodic** | What happened: conversations, events, decisions | `Memory/Conversations/`, [[Decision Journal]] | Summarized after 7 days |
| **Semantic** | Facts about Sir, the world, knowledge | `Memory/Core`, `Knowledge`, `People`, `Projects` | Long-term |
| **Procedural** | How to do things: coding playbooks, protocols | `Memory/Coding`, `Memory/Decisions` | Long-term, versioned |
| **Preference** | How Sir likes things done | `Memory/Preferences` | Until Sir changes it |

**Flow:** message arrives, retrieve (see [[Memory Retrieval]]), answer, extract new memories, write them, link them. Weekly: [[Memory Consolidation]].

**Rule of thumb:** facts go semantic, events go episodic, lessons go procedural. When an episode teaches a lesson, promote the lesson into a procedural note and link back to the episode.

## Related
- [[Memory Rules]]
- [[Memory Retrieval]]
- [[Memory Consolidation]]

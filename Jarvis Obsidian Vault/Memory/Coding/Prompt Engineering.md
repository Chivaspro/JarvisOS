---
title: "Prompt Engineering"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [prompts, llm, agents]
---

# Prompt Engineering

For Jarvis and anything built on an LLM:
- Put role, rules and format in the **system prompt**. Keep it short and specific.
- Show **1 to 3 examples** of the exact output wanted.
- Ask for **structured output** (JSON schema) when code consumes it, and validate it.
- Give tools **clear names, descriptions and typed arguments**.
- Tell the model what to do when unsure (ask, or say so), not just what to do when sure.
- Keep untrusted text (web, files) inside clearly marked data sections and tell the model not to follow instructions in them.
- Evaluate with a fixed test set before and after every prompt change.

## Related
- [[AI Agent Patterns]]
- [[Security Checklist]]
- [[Evaluation Suite]]

---
title: "Code Standards"
category: coding
jarvis: true
source: setup-interview
confidence: 0.95
confidence_label: high
created: 2026-10-16 09:00
updated: 2026-10-16 09:00
tags: [standards, style, quality]
---

# Code Standards

- Clear names. Small functions that do one thing.
- Handle errors on purpose: catch specific exceptions, give useful messages, never swallow errors silently.
- Validate all input at the edges.
- No hardcoded secrets. Use environment variables or a config file kept out of git.
- Type hints / types where the language supports them.
- Comments explain **why**, not what.
- Add tests for logic-heavy code. Keep them fast.
- Use the language's standard formatter and linter (black/ruff, prettier/eslint, gofmt, rustfmt).
- Prefer the standard library before adding a dependency. Pin versions of dependencies you do add.
- Log with levels instead of print in anything beyond a quick script.

## Related
- [[Coding Brain]]
- [[Code Review Checklist]]

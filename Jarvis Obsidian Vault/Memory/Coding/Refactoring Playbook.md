---
title: "Refactoring Playbook"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [refactoring, cleanup, quality]
---

# Refactoring Playbook

Refactor only with tests or a clear way to verify behavior.
1. Lock behavior with tests.
2. Small steps: rename, extract function, remove duplication, simplify conditions.
3. Run tests after each step.
4. Smells to fix: giant functions, deep nesting, magic numbers, copy-paste, unclear names, global state.
5. Prefer early returns and guard clauses over nested `if`s.
6. Stop when it's clear. Don't rewrite what works just because it's old.

## Related
- [[Code Standards]]
- [[Testing Strategy]]

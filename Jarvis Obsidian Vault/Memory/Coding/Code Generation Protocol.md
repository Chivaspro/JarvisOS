---
title: "Code Generation Protocol"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [codegen, process, advanced]
---

# Code Generation Protocol

For anything bigger than a snippet, Jarvis works like a senior engineer:

1. **Clarify** the goal in one line, state assumptions.
2. **Design** the file layout and main interfaces first (3 to 6 lines).
3. **Skeleton:** write the structure with real function signatures.
4. **Fill in** each part completely. No placeholders.
5. **Self-review** against [[Code Review Checklist]], looking for edge cases and failure modes.
6. **Test:** provide at least a few runnable tests or example calls.
7. **Deliver:** code, short explanation, run command, one upgrade idea.
8. **Remember:** save useful new patterns as notes.

If the request is huge, deliver a working core first, then extend in steps.

## Related
- [[Coding Brain]]
- [[Code Review Checklist]]
- [[Testing Strategy]]

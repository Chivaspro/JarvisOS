---
title: "Debugging Protocol"
category: coding
jarvis: true
source: setup-interview
confidence: 0.95
confidence_label: high
created: 2026-10-16 09:00
updated: 2026-10-16 09:00
tags: [debugging, errors, fix]
---

# Debugging Protocol

1. **Read the error** from the bottom of the traceback up. The real cause is usually near the last line of Sir's own code.
2. **Reproduce** it with the smallest possible input.
3. **Form one hypothesis**, test it, don't change five things at once.
4. **Fix the root cause**, not the symptom.
5. **Explain** the cause in one or two lines.
6. **Verify:** run it again, then check edge cases (empty, None, huge, wrong type).
7. **Remember:** if the bug pattern is common, save it as a memory so it's caught faster next time.

Stay calm in tone. A bug is information, not a disaster.

## Related
- [[Coding Brain]]
- [[Learning Loop]]

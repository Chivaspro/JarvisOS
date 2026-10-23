---
title: "Build Vs Buy"
category: decisions
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [tech-choice, dependencies, strategy]
---

# Build Vs Buy

Before writing something new, ask:
1. Does a solid open-source library or service already do it? (Use it.)
2. Is it core to what makes Jarvis special? (Build it.)
3. How long will it take vs how long to integrate? Count maintenance too.
4. Is the library maintained, popular and properly licensed?
5. Can it be swapped later? Wrap it behind your own interface.

Default: **reuse commodity parts, build the differentiating parts.**

## Related
- [[Decision Framework]]
- [[Architecture Patterns]]

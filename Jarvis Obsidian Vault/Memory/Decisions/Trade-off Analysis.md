---
title: "Trade-off Analysis"
category: decisions
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [tradeoffs, scoring, options]
---

# Trade-off Analysis

Use a **weighted score** when options are close:

1. List criteria that matter (impact, effort, risk, speed, learning, cost).
2. Give each a weight (total 100).
3. Score each option 1 to 10 per criterion.
4. Multiply and sum. Highest total wins, unless a hard constraint is violated.

| Criterion (weight) | Option A | Option B |
|---|---|---|
| Impact (35) | 8 | 6 |
| Effort (25) | 5 | 8 |
| Risk (20) | 7 | 7 |
| Speed (20) | 6 | 9 |
| **Total** | 6.6 | 7.2 |

Then sanity check against gut feeling. If the result feels wrong, a criterion or weight is missing. Fix that instead of ignoring the math.

## Related
- [[Decision Framework]]
- [[Risk Assessment]]

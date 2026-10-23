---
title: "Performance Optimization"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [performance, profiling, speed]
---

# Performance Optimization

1. **Measure first.** Never guess. Use `cProfile`, `timeit`, `py-spy` or the language's profiler.
2. **Fix the algorithm** before micro-optimizing (O(n^2) to O(n log n) beats any tweak).
3. **Right data structure:** dict/set for lookups, deque for queues, heapq for top-k.
4. **Avoid repeated work:** cache, precompute, batch I/O.
5. **Vectorize** numeric work (NumPy) instead of Python loops.
6. **Push heavy work** to processes, native extensions or a faster language only when measured need exists.
7. **Re-measure** after every change and keep the change only if it helped.

Premature optimization wastes Sir's time. Correct and clear first.

## Related
- [[Coding Brain]]
- [[Concurrency Guide]]

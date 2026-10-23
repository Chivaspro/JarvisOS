---
title: "Concurrency Guide"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [concurrency, async, threads, performance]
---

# Concurrency Guide

| Workload | Use | Why |
|---|---|---|
| Waiting on network, disk, APIs | `asyncio` (or threads for blocking libraries) | Cheap, thousands of tasks |
| CPU-heavy (math, parsing, ML) | `multiprocessing` / `ProcessPoolExecutor` | Avoids the GIL |
| GUI apps | Worker thread plus signals/queues | Never block the UI thread |
| Simple background job | `threading.Thread` + `queue.Queue` | Easy and safe |

**Rules:** share as little state as possible, communicate with queues, put timeouts on everything, handle cancellation, never mutate shared data without a lock. For a voice assistant: separate audio capture, speech recognition, reasoning and speech output so each can run without blocking the others.

## Related
- [[Advanced Python Patterns]]
- [[Architecture Patterns]]

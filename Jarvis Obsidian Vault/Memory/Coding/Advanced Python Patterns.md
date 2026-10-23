---
title: "Advanced Python Patterns"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [python, advanced, patterns, asyncio]
---

# Advanced Python Patterns

```python
# 1. Dataclasses with validation
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass(slots=True)
class Memory:
    title: str
    text: str
    confidence: float = 0.8
    tags: list[str] = field(default_factory=list)
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

# 2. Protocols (structural typing) for swappable components
from typing import Protocol

class Searcher(Protocol):
    def search(self, query: str, k: int = 5) -> list[Memory]: ...

# 3. Context managers
from contextlib import contextmanager
import time

@contextmanager
def timer(label: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        print(f"{label}: {time.perf_counter() - start:.3f}s")

# 4. Async with a concurrency limit
import asyncio

async def run_all(coros, limit: int = 5):
    sem = asyncio.Semaphore(limit)
    async def guard(c):
        async with sem:
            return await c
    return await asyncio.gather(*(guard(c) for c in coros), return_exceptions=True)

# 5. Caching
from functools import lru_cache

@lru_cache(maxsize=1024)
def expensive(n: int) -> int:
    return sum(i * i for i in range(n))

# 6. Generators for large data
def read_lines(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            yield line.rstrip("\n")
```

**Also reach for:** `pathlib`, `itertools`, `collections.Counter/defaultdict/deque`, `enum`, `typing`, `logging`, `argparse`, `concurrent.futures`.

## Related
- [[Coding Brain]]
- [[Concurrency Guide]]
- [[Testing Strategy]]

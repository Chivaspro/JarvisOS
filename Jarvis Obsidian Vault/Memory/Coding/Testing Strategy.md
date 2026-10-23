---
title: "Testing Strategy"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [testing, pytest, quality]
---

# Testing Strategy

- Use **pytest**. Name tests `test_<behavior>`.
- **Arrange, Act, Assert.** One idea per test.
- Test the edges: empty, None, huge, wrong type, unicode, duplicates.
- **Fix a bug = add a test** that fails first, then passes.
- Mock only the outside world (network, clock, files), not your own logic.
- Use `pytest.mark.parametrize` for many cases cheaply.
- Keep tests fast so they actually get run.

```python
import pytest

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

@pytest.mark.parametrize("x,expected", [(-5, 0), (5, 5), (50, 10)])
def test_clamp(x, expected):
    assert clamp(x, 0, 10) == expected
```

## Related
- [[Code Generation Protocol]]
- [[Debugging Protocol]]

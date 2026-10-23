---
title: "Error Handling And Logging"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [errors, logging, reliability]
---

# Error Handling And Logging

```python
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis")

def safe_call(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (OSError, ValueError) as exc:
        log.warning("%s failed: %s", fn.__name__, exc)
        return default
    except Exception:
        log.exception("Unexpected error in %s", fn.__name__)
        raise
```
- Catch **specific** exceptions. Re-raise what you can't handle.
- Log with context, never log secrets.
- Retry transient failures with exponential backoff and a limit.
- Fail soft in the assistant (report, keep running), fail loudly in scripts.

## Related
- [[Code Standards]]
- [[Debugging Protocol]]

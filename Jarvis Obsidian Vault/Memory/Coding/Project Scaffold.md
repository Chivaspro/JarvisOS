---
title: "Project Scaffold"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [scaffold, structure, python]
---

# Project Scaffold

```
my_project/
  pyproject.toml
  README.md
  .env.example
  .gitignore
  src/my_project/
    __init__.py
    main.py
    config.py
    core/        # logic
    tools/       # plugins
    memory/      # storage and retrieval
  tests/
    test_core.py
```
Use a virtual environment (`python -m venv .venv`), `ruff` + `black` + `mypy`, and `pytest`. Entry point via `python -m my_project`. Config from environment variables with defaults in `config.py`.

## Related
- [[Architecture Patterns]]
- [[Code Standards]]

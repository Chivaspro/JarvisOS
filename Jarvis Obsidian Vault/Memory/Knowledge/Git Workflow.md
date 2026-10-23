---
title: "Git Workflow"
category: knowledge
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [git, github, workflow]
---

# Git Workflow

- Branch per feature: `git switch -c feature/short-name`.
- Small commits with clear messages: `feat: add memory search`, `fix: handle empty query`.
- Pull before push. Never force-push shared branches.
- `.gitignore` first: `.env`, `__pycache__/`, `*.log`, venvs.
- Tag releases: `git tag v0.2.0`.
- Undo safely: `git restore <file>`, `git revert <commit>`. Ask Sir before `reset --hard`.
- Keep a `README.md` with what it is, how to run it, and a screenshot.

## Related
- [[Coding Brain]]
- [[Security Checklist]]

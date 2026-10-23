---
title: "Database Choices"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [database, sql, storage]
---

# Database Choices

| Need | Pick |
|---|---|
| Quick, local, single user | **SQLite** (zero setup, great default) |
| Multi-user app, relations | **PostgreSQL** |
| Cache, queues, sessions | **Redis** |
| Flexible documents | MongoDB / JSON columns in Postgres |
| Semantic memory search | pgvector, Chroma or FAISS |
| Plain notes, human-readable | Markdown files (like this vault) |

Always use parameterized queries. Add indexes for columns you filter on. Migrate with a tool (Alembic) instead of ad-hoc edits.

## Related
- [[Architecture Patterns]]
- [[RAG And Embeddings]]

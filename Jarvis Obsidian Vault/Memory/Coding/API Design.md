---
title: "API Design"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [api, design, rest]
---

# API Design

- Resource-based URLs (`/notes`, `/notes/{id}`), HTTP verbs used correctly.
- Consistent JSON shapes and error format: `{"error": {"code": "...", "message": "..."}}`.
- Validate input (pydantic / schema), return correct status codes (200, 201, 400, 401, 404, 429, 500).
- Version early (`/v1/`), paginate lists, rate-limit, authenticate everything non-public.
- Idempotent PUT/DELETE, safe retries with idempotency keys for payments or critical writes.
- Document with OpenAPI (FastAPI generates this for free).

## Related
- [[Architecture Patterns]]
- [[Security Checklist]]

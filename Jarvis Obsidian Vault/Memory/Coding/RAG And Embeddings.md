---
title: "RAG And Embeddings"
category: coding
jarvis: true
source: brain-upgrade-v2
confidence: 0.90
confidence_label: high
created: 2026-10-16 11:00
updated: 2026-10-16 11:00
tags: [rag, embeddings, semantic, memory]
---

# RAG And Embeddings

How to upgrade Jarvis memory from keywords to meaning:

1. **Chunk** each note by heading (about 200 to 500 words).
2. **Embed** chunks with an embedding model (local `sentence-transformers` or an API).
3. **Store** vectors in FAISS / Chroma / SQLite + sqlite-vec, along with note path and confidence.
4. **Query:** embed the question, take the top 10 by cosine similarity.
5. **Re-rank** with confidence, recency and category (see [[Memory Retrieval]]).
6. **Inject** the best chunks into the prompt within the context budget.
7. **Re-index** only changed notes (compare `updated` time).

Keep keyword search as a fallback: hybrid search beats either alone. Cite which notes were used so Sir can trust or correct them.

## Related
- [[Memory Retrieval]]
- [[Database Choices]]
- [[Jarvis Roadmap]]

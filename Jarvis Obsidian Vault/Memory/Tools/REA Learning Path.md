---
title: "REA Learning Path"
category: tools
jarvis: true
source: brain-upgrade-v3
confidence: 0.80
confidence_label: high
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, learning, roadmap, reverse-engineering]
---

# REA Learning Path

An 8-week path so Sir can do this himself, not just ask Jarvis:

| Week | Learn | Practice |
|---|---|---|
| 1 | How programs run: compile, link, memory, stack, heap | Write small C programs, compile, run |
| 2 | Basic C and pointers | Reimplement `strlen`, `strcpy` |
| 3 | Assembly basics (x86-64 registers, mov/add/cmp/jmp, calls) | Compile with `-S`, read the assembly |
| 4 | Ghidra basics: import, functions, decompiler window | Open your own compiled program, find `main` |
| 5 | JavaScript/Electron structure: main, preload, renderer, IPC | Analyze your own small Electron app with REA |
| 6 | Network and web: HAR files, browser dev tools | Capture your own app's traffic, read requests |
| 7 | Verify and rebuild: write a function from observed behavior | Reconstruct a small function and compare outputs |
| 8 | Full case study with a case file | Pick an open-source app, document how one feature works |

**Habits:** keep a case file for every session, explain findings out loud in simple words, and always check claims with a test. Use legal practice sites (picoCTF, pwn.college).

## Related
- [[Learning Strategy]]
- [[REA Complete Guide]]
- [[REA Case File Template]]

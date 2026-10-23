---
title: "REA Complete Guide"
category: tools
jarvis: true
source: brain-upgrade-v3
confidence: 0.70
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, guide, tutorial, learning]
---

# REA Complete Guide

# REA Complete Guide (for Sir)

This guide teaches you to understand and use REA yourself, step by step. Read it once, then do the exercises.

## 1. What is reverse engineering?
Normally a developer writes **source code**, and a tool turns it into a **program** (a binary or a packaged app). **Reverse engineering** goes backward: you start from the finished program and figure out how it works. People do it for security research, learning, fixing compatibility, preserving old software and understanding file formats.

Two styles:
- **Static analysis:** read the program's files without running it. Safe and repeatable.
- **Dynamic / runtime analysis:** run it and watch what it does. Powerful but riskier.

## 2. Words you will see
| Word | Plain meaning |
|---|---|
| Binary | A compiled program (.exe, ELF, Mach-O) |
| Assembly | Human-readable form of the CPU instructions |
| Decompiler | Tool that turns instructions back into C-like "pseudocode" |
| Symbol | A name (function or variable) left inside a program |
| Electron app | A desktop app built from web tech (JavaScript, HTML), e.g. Notion, VS Code |
| Renderer / preload / main | The three parts of an Electron app (UI, bridge, system side) |
| IPC | How those parts send messages to each other |
| ASAR | Electron's package format for app files |
| MCP | A standard way for an AI agent to call tools |
| Agent | An AI that can use tools, like Jarvis |
| Evidence | The proof for a claim: code location, string, network request |

## 3. What REA adds
REA is a bridge between your AI agent and the real analysis tools. Instead of you clicking through Ghidra for hours, the agent asks REA to inspect the target, trace the code, and report back **with evidence and limitations**. You can still use the same tools from the terminal.

## 4. Rules before you start
Only analyze software you **own** or are **allowed** to examine. Never use it to crack, cheat, steal or attack. Read [[REA Safety And Legal]]. When in doubt, ask first.

## 5. Install (10 minutes)
1. Install Node.js (version 22.19+, 24.11+ or 26+) from nodejs.org. Check: `node --version`.
2. Run `npx rea-agents setup`.
3. Choose your agent(s), read the changes it proposes, approve.
4. **Restart your agent.**
5. Install the terminal command: `npm install --global rea-agents`, then `rea --help`.
6. Update often: `rea update`.

## 6. Exercise 1: analyze your own app (JavaScript/Electron)
No extra tools needed.
1. Make or find a small Electron app you own (or an open-source one).
2. Run:
```
npx -y rea-agents@latest analyze-javascript-application /absolute/path/to/app --json
```
3. Read the JSON. Find: the list of **modules**, their **imports**, and the **Electron boundaries**.
4. Write 5 sentences in your own words: what does each part do?

## 7. Exercise 2: ask the agent
After setup, tell your agent:
> Understand how [one specific feature] works in [my app], show me the evidence, and tell me what you could not determine.

Check the answer: does every claim point to real code? If not, ask "where is the evidence for that?"

## 8. Native programs (the big league)
Native analysis needs **Ghidra** (free), **Hopper** or **IDA** (commercial, must be licensed legally). Start with Ghidra.
1. Write a tiny C program with a function that does some math. Compile it.
2. Open it in Ghidra, find `main`, read the decompiler output.
3. Then ask your agent (with REA) to explain the same function and compare.
4. Learn assembly basics alongside: registers, `mov`, `add`, `cmp`, `jmp`, `call`.

## 9. Read results like a detective
- **Evidence beats opinion.** A claim without a location is a guess.
- **Decompiled code can lie.** It is a reconstruction. Cross-check with assembly.
- **Unknowns are information.** Obfuscation, stripped symbols and dynamic code limit what static analysis can see.
- **Verify by rebuilding.** Write your own version of a function and test it on many inputs against the original behavior.

## 10. Build your own version (clean-room)
Follow [[REA Clean Room Method]]: observe, write a spec in your words, then build from the spec. This keeps your work original and legal.

## 11. Case studies to read (from REA's showcases)
- **DX-Ball:** following a sound call into a helper that computes stereo pan, then rebuilding it in C.
- **Notion:** tracing the Electron clipboard bridge from renderer through preload and IPC to the main process.
- **TH04:** recovering a bullet-pattern calculation from a 16-bit DOS game.
Find them at rea.tools/showcase.

## 12. Use it with Jarvis
See [[REA With Jarvis]]. Jarvis checks authorization, picks the cheapest analysis, asks REA, summarizes the evidence honestly, and saves a case file.

## 13. When something breaks
See [[REA Troubleshooting]]. First step is always `rea update` and a restart.

## 14. Your 8-week plan
See [[REA Learning Path]]. Do a little every day. Keep a case file for each investigation ([[REA Case File Template]]).

## 15. Self-check questions
1. What is the difference between static and runtime analysis, and which is safer?
2. What are the three parts of an Electron app?
3. Why must a conclusion come with evidence?
4. What does a decompiler produce, and why might it be wrong?
5. What is a clean-room rebuild?
6. When must you stop and not analyze something?
7. What should you do first if REA misbehaves?
8. Why start with static analysis?

**Answers:** (1) static reads files and is safer; runtime executes the program. (2) renderer, preload, main. (3) so you can check it instead of trusting a guess. (4) pseudocode reconstructed from instructions; it can miss or misrepresent details. (5) learn behavior, write a spec, build independently. (6) when you don't own it or aren't authorized, or the goal is cracking, cheating or harm. (7) update and restart. (8) cheaper, repeatable, no risk of running unknown code.

## Related
- [[REA Overview]]
- [[REA Learning Path]]
- [[REA Cheat Sheet]]

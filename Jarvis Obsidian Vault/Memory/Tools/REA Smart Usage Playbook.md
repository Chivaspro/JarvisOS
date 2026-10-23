---
title: "REA Smart Usage Playbook"
category: tools
jarvis: true
source: brain-upgrade-v3
confidence: 0.85
confidence_label: high
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, strategy, decisions, smart]
---

# REA Smart Usage Playbook

How Jarvis uses REA intelligently, Sir:

**1. Gate:** confirm authorization ([[REA Safety And Legal]]). If not clear, stop and ask.

**2. Choose the lowest-risk, lowest-cost path (ladder)**
- Websites, JS/Electron, .NET, HAR files, APK manifests: **no extra tools, static**. Start here.
- Native binaries: needs Hopper/Ghidra/IDA. Start with strings, symbols, imports, then pseudocode of one function.
- Runtime capture: last resort, in a VM.

**3. Ask sharp questions.** "How does X work?" beats "analyze this". Narrow questions give faster, more accurate answers.

**4. Demand evidence.** Every conclusion needs a pointer (module, function, string, request). If REA reports unknowns or limitations, Jarvis repeats them honestly and gives a confidence level ([[Confidence Calibration]]).

**5. Iterate in small steps:** survey, hypothesis, trace, verify, then move on. Do not trust one decompiled output blindly. Pseudocode can be wrong.

**6. Save knowledge.** Put each investigation into a case file note, then add atomic findings as memories with confidence. Next time Jarvis recalls them before re-analyzing.

**7. Keep REA current.** Run `rea update` weekly, because it ships fixes often.

**8. Decision rule:** if Sir just wants a feature idea, use docs/open-source first. Use REA only when the answer truly needs inspecting software Sir is allowed to inspect.

## Related
- [[Decision Framework]]
- [[REA Workflows]]
- [[REA Target Types]]
- [[Memory Rules]]

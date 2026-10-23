---
title: "REA Workflows"
category: tools
jarvis: true
source: external-readme
confidence: 0.60
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, workflows, investigation]
---

# REA Workflows

**Prompt template (from the REA README, adapt it):**
> Understand how **[feature]** works in **[app you own]**, show me the evidence, and build a similar feature for my project.

**Investigation workflow**
1. **Scope:** one clear question ("how does X save files?"), not "analyze everything".
2. **Pick the cheapest target type** (see [[REA Target Types]]). Static first.
3. **Survey:** modules, strings, imports, entry points.
4. **Trace:** follow the feature from the UI or entry point down to the core function.
5. **Evidence:** every claim must point to code, strings or captured data.
6. **Limits:** note what REA could not see (obfuscation, missing symbols, dynamic code).
7. **Verify:** test your understanding (re-implement a function and compare outputs).
8. **Record:** save findings in a case file ([[REA Case File Template]]).

**Terminal quick start (JavaScript/Electron app folder or ASAR)**
```
npx -y rea-agents@latest analyze-javascript-application /absolute/path/to/app --json
```
Look for: modules, imports, Electron boundaries (renderer, preload, main) and their evidence.

> Source: the official REA README (github.com/morluto/rea) and rea.tools. REA changes quickly, so verify details there before relying on them.

## Related
- [[REA Smart Usage Playbook]]
- [[REA Target Types]]
- [[REA Case File Template]]

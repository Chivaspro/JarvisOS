---
title: "REA Cheat Sheet"
category: tools
jarvis: true
source: external-readme
confidence: 0.60
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, commands, cheatsheet]
---

# REA Cheat Sheet

```
node --version                          # needs 22.19+, 24.11+ or 26+
npx rea-agents setup                    # register REA with your agent, then restart it
npx rea-agents@latest setup             # refresh after an update
npm install --global rea-agents         # install the `rea` command
rea --help                              # list commands
rea update                              # update the CLI
npx -y rea-agents@latest analyze-javascript-application /abs/path/to/app --json
```
**Prompt starters**
- "Understand how [feature] works in [my app], show the evidence and limitations."
- "List the Electron main, preload and renderer modules and how they talk (IPC)."
- "What does this function do? Show the pseudocode and the assembly it came from."
- "Compare these two builds and tell me what changed."

Run `rea --help` for native and other commands, since the list grows with updates.

> Source: the official REA README (github.com/morluto/rea) and rea.tools. REA changes quickly, so verify details there before relying on them.

## Related
- [[REA Setup]]
- [[REA Workflows]]

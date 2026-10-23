---
title: "REA Troubleshooting"
category: tools
jarvis: true
source: external-readme
confidence: 0.60
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, troubleshooting, errors]
---

# REA Troubleshooting

1. **Update first:** `rea update`, then `npx rea-agents@latest setup`, then restart your agent.
2. **Node version:** run `node --version` (needs 22.19+, 24.11+ or 26+).
3. **Agent doesn't see REA:** you must restart the agent after setup. Check the agent's MCP list.
4. **Native analysis fails:** make sure Hopper, Ghidra or IDA is installed and selected as the provider (see the CLI guide). On macOS, Hopper may show a first-run dialog (demo or license). Finish it first.
5. **Big binary times out:** raise `REA_GHIDRA_STARTUP_TIMEOUT_MS` or `REA_HOPPER_STARTUP_TIMEOUT_MS`.
6. **Linux errors about processes:** install a procps-compatible `ps`.
7. **Windows paths:** use absolute paths like `D:/apps/example`.
8. **Still broken:** open an issue on GitHub with your REA version, target type, steps and the error text.
9. **Is the download real?** Only use morluto/rea, rea.tools or the npm package `rea-agents`. Lookalike repos exist.

> Source: the official REA README (github.com/morluto/rea) and rea.tools. REA changes quickly, so verify details there before relying on them.

## Related
- [[REA Setup]]
- [[Debugging Protocol]]

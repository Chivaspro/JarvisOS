---
title: "REA Setup"
category: tools
jarvis: true
source: external-readme
confidence: 0.60
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, install, setup, node]
---

# REA Setup

**Requirements:** Node.js 22.x (22.19 or newer), 24.x (24.11 or newer), or 26+, plus npm. Check with `node --version`.

**Install for your agent**
```
npx rea-agents setup
```
Pick your agent, **read the proposed changes**, approve, then **restart the agent**. Setup adds the MCP server and workflow instructions and makes backups of old config.

**Use from the terminal**
```
npm install --global rea-agents
rea --help
```

**Update (REA changes fast)**
```
rea update
npx rea-agents@latest setup
```

**Extra tools depend on the target.** Native binaries need one of Hopper, Ghidra or IDA (Ghidra is free and open source). Static JavaScript and .NET analysis need nothing extra. Android APKs need JADX and a JDK. On Linux some analysis needs a procps-compatible `ps`.

**Big binaries time out?** Raise `REA_GHIDRA_STARTUP_TIMEOUT_MS` or `REA_HOPPER_STARTUP_TIMEOUT_MS`.

> Source: the official REA README (github.com/morluto/rea) and rea.tools. REA changes quickly, so verify details there before relying on them.

## Related
- [[REA Overview]]
- [[REA Troubleshooting]]
- [[REA With Jarvis]]

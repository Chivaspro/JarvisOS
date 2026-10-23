---
title: "REA Overview"
category: tools
jarvis: true
source: external-readme
confidence: 0.60
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, reverse-engineering, mcp, overview]
---

# REA Overview

**REA = "Reverse Engineer Anything."** An open-source (MIT) tool by morluto: https://github.com/morluto/rea, site https://rea.tools, npm package `rea-agents`.

**In one sentence:** it connects an AI agent (like Jarvis) to real reverse-engineering tools so the agent can look inside an app, explain how a feature works, **show the evidence**, and help you build your own version.

**How it connects:** through **MCP** (Model Context Protocol, a standard way for agents to use tools) or through a normal **terminal command** (`rea`).

**What it can look at:** native programs (binaries), JavaScript/Electron apps, .NET programs, websites, Android apps, network captures, firmware and more. See [[REA Target Types]].

**Key promises:** analysis runs **locally** on your computer, and results come with **evidence and limitations**, so you can check the agent instead of trusting it blindly.

**Important:** use it only on software you own or are authorized to examine. See [[REA Safety And Legal]].

> Source: the official REA README (github.com/morluto/rea) and rea.tools. REA changes quickly, so verify details there before relying on them.

## Related
- [[REA Setup]]
- [[REA Target Types]]
- [[REA Safety And Legal]]
- [[REA Complete Guide]]

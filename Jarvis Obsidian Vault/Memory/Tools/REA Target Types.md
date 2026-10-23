---
title: "REA Target Types"
category: tools
jarvis: true
source: external-readme
confidence: 0.60
confidence_label: medium
created: 2026-10-16 14:00
updated: 2026-10-16 14:00
tags: [rea, targets, analysis]
---

# REA Target Types

| What you want to inspect | What REA gives back | Extra needs |
|---|---|---|
| Native programs (.exe, ELF, Mach-O) | Pseudocode, assembly, strings, symbols, calls | Hopper, Ghidra or IDA |
| JavaScript / Electron apps | Modules, imports, source maps, routes, IPC | Node + npm |
| .NET assemblies | Metadata, CIL instructions, dependencies | None (static) |
| Websites | Page structure, scripts, network, screenshots | Chrome-family browser |
| Saved network captures (HAR, SAZ) | Requests, responses, payloads | None for HAR |
| Android APKs | Manifest, classes, decompiled methods | JADX + JDK |
| Android devices | Packages, processes, logs, file transfer | adb |
| Go / Rust binaries | Compiler version, modules, crates | None (built-in) |
| SQLite databases | Tables, columns, rows | Node 24.15+ |
| Firmware | Regions, extraction | Binwalk / Unblob (Linux) |
| Packets / pcap | Endpoints, DNS, TLS, HTTP | tshark (Linux) |
| Flutter apps | Dart snapshot hash, engine info | None |
| Process behavior | Terminal output, file effects | Linux/macOS |

**Static** analysis reads files without running them (safer). **Runtime capture** runs the target with your user permissions (riskier, use a VM for untrusted things).

> Source: the official REA README (github.com/morluto/rea) and rea.tools. REA changes quickly, so verify details there before relying on them.

## Related
- [[REA Overview]]
- [[REA Smart Usage Playbook]]
- [[REA Safety And Legal]]

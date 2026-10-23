---
title: "Threat Detection"
category: guardian
jarvis: true
source: brain-upgrade-v4
confidence: 0.85
confidence_label: high
created: 2026-10-16 16:00
updated: 2026-10-16 16:00
tags: [guardian, detection, monitoring, ioc]
---

# Threat Detection

What "something looks wrong" means, concretely:

**On your machine**
- Unknown process using high CPU/network, or running from a weird path (Temp, Downloads, hidden folder).
- A program set to auto-start that you didn't add.
- Antivirus/Defender disabled without you doing it.
- Unexpected outbound connections, especially to unfamiliar IPs or odd ports.
- New admin/user accounts you didn't create.
- Files encrypted, renamed in bulk, or a ransom note appears.
- Browser homepage, extensions or search engine changed without you.

**On accounts**
- Login alerts from unfamiliar locations or devices.
- Password reset emails you didn't request.
- Sent emails/messages you didn't send.
- Can't log in (possible takeover).

**On a server/website**
- Spike in traffic to odd files, repeated failed logins, new files in upload folders, modified core files, unknown scheduled tasks/cron jobs.

**Severity:** Low = one odd sign, no damage. Medium = an unwanted process or account, no data loss confirmed. High = ransomware, confirmed account takeover, active data theft. High goes straight to [[Incident Response]] "contain" step.

## Related
- [[Guardian Overview]]
- [[Incident Response]]
- [[Guardian With Jarvis]]

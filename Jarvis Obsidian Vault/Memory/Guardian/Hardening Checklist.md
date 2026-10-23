---
title: "Hardening Checklist"
category: guardian
jarvis: true
source: brain-upgrade-v4
confidence: 0.85
confidence_label: high
created: 2026-10-16 16:00
updated: 2026-10-16 16:00
tags: [guardian, hardening, prevention]
---

# Hardening Checklist

Do these once, then review every few months, Sir:

**Accounts**
- [ ] Unique password per account (use a password manager).
- [ ] 2FA (authenticator app, not SMS when possible) on email, banking, GitHub, socials.
- [ ] Recovery email/phone up to date.

**Device**
- [ ] OS and software auto-update on.
- [ ] Firewall on. Antivirus/Defender on and not disabled.
- [ ] Full-disk encryption (BitLocker/FileVault) especially on laptops.
- [ ] Regular backups, at least one **offline or separate** copy (so ransomware can't reach it).
- [ ] Standard user account for daily use, admin only when needed.

**Network**
- [ ] Router admin password changed from default, firmware updated.
- [ ] Wi-Fi uses WPA2/WPA3 with a strong password.
- [ ] Guest network for visitors/IoT devices, separate from your main devices.

**Behavior**
- [ ] Don't reuse passwords. Don't click unexpected links/attachments. Verify before entering credentials (check the real URL).
- [ ] Review installed apps/extensions periodically, remove unused ones.

**Dev-specific (since Sir codes)**
- [ ] `.env` and secrets never committed (see [[Security Checklist]]).
- [ ] Dependencies kept updated, review before installing new ones.
- [ ] 2FA + SSH keys (not just password) for GitHub and servers.

## Related
- [[Guardian Overview]]
- [[Security Checklist]]
- [[REA Safety And Legal]]

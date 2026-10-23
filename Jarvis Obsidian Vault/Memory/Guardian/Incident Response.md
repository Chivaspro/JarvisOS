---
title: "Incident Response"
category: guardian
jarvis: true
source: brain-upgrade-v4
confidence: 0.85
confidence_label: high
created: 2026-10-16 16:00
updated: 2026-10-16 16:00
tags: [guardian, incident, response, containment]
---

# Incident Response

The standard order, Sir (same shape professionals use):

1. **Identify:** what happened, what system, since when. Use [[Threat Detection]] signs.
2. **Contain:** stop the bleeding without destroying evidence.
   - Device: disconnect from Wi-Fi/network (don't shut down yet if ransomware — memory may hold the key).
   - Account: change the password from a **different, clean** device, enable 2FA, log out other sessions.
   - Server: take the affected service offline or isolate it; don't delete logs.
3. **Eradicate:** remove the malware/backdoor/unauthorized account (see [[Malware Removal]]).
4. **Recover:** restore from a known-clean backup, reinstall if unsure, change every password that device/account could see, update software.
5. **Review:** how did it get in, how do you prevent it (see [[Hardening Checklist]]). Log the incident like a [[Decision Journal]] entry.

**Ransomware specifically:** don't pay if avoidable, disconnect network, don't reboot (memory may help recovery), check nomoreransom.org for a free decryptor, restore from backup once clean.

**If money, identity or a minor is involved:** stop and involve a parent/guardian, your bank, and local authorities (in the Philippines: the PNP Anti-Cybercrime Group). This is beyond what Jarvis alone should handle.

## Related
- [[Guardian Overview]]
- [[Malware Removal]]
- [[Guardian Rules Of Engagement]]

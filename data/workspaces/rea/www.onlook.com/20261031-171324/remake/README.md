# Onlook Clean-Room Remake

This is a clean-room, zero-dependency reproduction of the [Onlook](https://www.onlook.com) marketing homepage and visual engineer canvas editor, constructed from DOM structural analysis and observed traffic patterns.

## Features
- **Visual Studio Simulator**: Interactive canvas with component selection, in-place text editing, live Tailwind CSS class generation, radius/padding adjustment sliders, and click metrics.
- **Studio Shimmer Gradient**: Recreation of Onlook's signature text shimmer CSS clip animation.
- **Local-first Telemetry & Auth Mock**: Lightweight Python HTTP server replicating the `/api/auth/get-session` and GitHub statistics endpoints observed in traffic.
- **Zero Third-party Trackers**: All external marketing tracking scripts (TikTok, Meta, Google Ads) stripped for pure clean-room compliance.

## Quick Start
1. Run the Python mock backend:
   ```bash
   python3 server.py
   ```
2. Open your browser at `http://localhost:8080`.
```

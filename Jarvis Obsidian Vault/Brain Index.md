# Jarvis Brain v2

Start at [[Memory Index]]. Core behavior lives in [[System Prompt]].

Commands (run from the vault folder):
- `python jarvis_brain_loader.py prompt` prints the system prompt
- `python jarvis_brain_loader.py context "question"` builds a memory block to inject
- `python jarvis_brain_loader.py search "query"`, `add`, `reinforce`, `supersede`, `forget`, `links`
- `python jarvis_brain_loader.py health` and `consolidate` for weekly maintenance
- `python jarvis_brain_loader.py index` rebuilds the Memory Index

# Jarvis Brain Pack v2

Extract this zip over your "Jarvis Obsidian Vault" folder (existing notes are kept).

## Connect it to Jarvis (Python)
```python
from jarvis_brain_loader import system_prompt, context, add_memory

messages = [
    {"role": "system", "content": system_prompt() + "\n\n# Relevant memories\n" + context(user_message)},
    {"role": "user", "content": user_message},
]
# after the reply, save anything worth remembering:
# add_memory("Short title", "The fact", category="preferences", confidence=0.95)
```

## Weekly routine
1. `python jarvis_brain_loader.py health`
2. `python jarvis_brain_loader.py consolidate`
3. `python jarvis_brain_loader.py index`

Notes never store passwords, keys, card or ID numbers (the loader refuses them).

## REA (reverse engineering) tools
Start with `Memory/Tools/REA Complete Guide.md`. `jarvis_rea.py` lets Jarvis call the REA command line safely
(authorization gate, timeout, audit log). Only analyze software you own or are allowed to examine.

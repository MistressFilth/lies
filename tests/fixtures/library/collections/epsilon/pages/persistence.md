---
title: "Hint persistence"
tags: [plugins, hints, persistence]
---

The CLI persists "hint dismissed" state across sessions in `~/.config/<cli>/hints.seen.json`. Dismissed hints do not reappear. To reset: delete the file (CLI will recreate on next session).

The persistence file is per-CLI and per-user; it is not synced across machines.

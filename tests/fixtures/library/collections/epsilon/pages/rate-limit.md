---
title: "Hint rate limit"
tags: [plugins, hints, rate-limit]
---

Across all CLIs on the machine, **at most one hint prompt appears per session**. The CLI tracks which hints have been shown via `~/.config/<cli>/hints.seen.json` and skips repeats.

Once per session is enforced at the daemon level; plugins cannot bypass it.

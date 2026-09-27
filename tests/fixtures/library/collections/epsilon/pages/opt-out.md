---
title: "Disabling hints"
tags: [plugins, hints, opt-out]
---

Disable hints globally: `plugin hints --disable`. Per-plugin: `plugin hints --disable <name>`. Per-session: `export <CLI>_HINTS=off`.

Disabled hints are still counted in the rate-limit budget; opting out means the budget does not consume.

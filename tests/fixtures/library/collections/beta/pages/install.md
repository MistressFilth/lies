---
title: "Installing plugins"
tags: [plugins, install, marketplace]
---

Install: `plugin install <name>`. To install from a private repo: `plugin install github.com/org/plugin`. To install from a local path: `plugin install ./my-plugin`.

The install command resolves dependencies, prompts for scope consent, and writes to `~/.config/<cli>/plugins/`. Uninstall: `plugin uninstall <name>`.

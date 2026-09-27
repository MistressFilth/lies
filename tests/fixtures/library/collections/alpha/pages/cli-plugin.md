---
title: "CLI plugin overview"
tags: [plugins, cli, authoring]
---

The CLI plugin model treats every command as a plugin. To author one, define a `Plugin` module that exports `setup(ctx)` and registers hooks.

CLI plugins extend the agent loop, intercept input events, and surface TUI components. The CLI loads them automatically at startup.

---
title: "CLI plugin runtime"
tags: [plugins, cli, runtime]
---

At runtime, the CLI keeps a registry of loaded plugins. Each plugin's `setup(ctx)` runs once; subsequent plugin invocations share the same context. Plugin state persists across commands via `ctx.storage`.

The runtime logs plugin load failures at WARN; the CLI does not crash on a single plugin error.

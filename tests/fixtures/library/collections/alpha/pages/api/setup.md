---
title: "setup(ctx) lifecycle"
tags: [plugins, api, setup, authoring]
---

The `setup(ctx)` callback runs once at plugin load time. `ctx` exposes hook registration methods (`ctx.on(event, handler)`), storage (`ctx.storage.get(key)`), and the logger (`ctx.logger.info(msg)`).

Authoring setup correctly means registering all event listeners synchronously — async work belongs in handlers, not in `setup` itself.

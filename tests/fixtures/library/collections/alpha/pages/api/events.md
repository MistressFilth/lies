---
title: "Event hooks reference"
tags: [plugins, api, events]
---

Plugin events: `session.start`, `session.end`, `message.received`, `tool.execute.before`, `tool.execute.after`. Subscribe via `ctx.on(event, handler)`.

Authoring event-driven plugins: each handler runs synchronously; long-running work should be offloaded to background tasks via `ctx.spawn`.

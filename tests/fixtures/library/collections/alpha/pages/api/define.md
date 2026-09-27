---
title: "Plugin.define API"
tags: [plugins, api, authoring]
---

`Plugin.define({ id, setup })` returns a plugin module. The `id` field is required and must be unique across all loaded plugins. The `setup(ctx)` callback receives a context object with hook registration methods.

Authoring via `Plugin.define` is the recommended entry point. The legacy `module.exports = setup` form still works but lacks the typed hook surface.

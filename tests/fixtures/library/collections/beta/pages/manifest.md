---
title: "Plugin manifest format"
tags: [plugins, manifest, marketplace]
---

The marketplace manifest (`plugin.json` or `.claude-plugin/plugin.json`) declares the plugin's id, version, author, and entry point. A plugin without a manifest uses the default discovery rules: a top-level `index.ts` exporting `Plugin.define`.

Manifest fields: `id`, `version`, `name`, `description`, `author`, `entry`. All required except `entry` which defaults to `index.ts`.

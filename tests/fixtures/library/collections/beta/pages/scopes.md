---
title: "Plugin scopes"
tags: [plugins, scopes, marketplace]
---

A scope is a permission boundary the user grants at install time. Scopes: `filesystem.read`, `filesystem.write`, `network.outbound`, `process.spawn`. The manifest declares requested scopes; the install command prompts the user to confirm.

A plugin with `process.spawn` scope can run arbitrary shell commands on the user's machine. Scopes are not enforced at runtime; they are install-time consent.

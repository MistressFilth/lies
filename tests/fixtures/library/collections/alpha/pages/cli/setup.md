---
title: "CLI plugin loading"
tags: [plugins, cli, loading]
---

The CLI loads plugins from `.opencode/plugins/` (project) and `~/.config/opencode/plugins/` (global). Each `.ts` or `.js` file is one plugin. Authoring CLI plugins means dropping a file into one of these directories.

Load order: project first, then global. Later plugins cannot override earlier plugin IDs.

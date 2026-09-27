---
title: "Distribution channels"
tags: [plugins, distribution, marketplace]
---

Plugins distribute via npm packages or git repositories. The marketplace CLI handles dependency resolution, signature verification, and scope validation.

Distribution flow: author publishes to npm → marketplace indexes → user installs via `plugin install <name>` → CLI loads from `~/.cache/.../node_modules/<name>/`.

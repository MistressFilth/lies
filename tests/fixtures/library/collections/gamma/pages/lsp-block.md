---
title: "lsp block schema"
tags: [plugins, lsp, schema]
---

The `lsp` block accepts per-server entries keyed by an arbitrary name (the "server id"). Each entry has:

- `command` (string, required): binary name or absolute path
- `args` (list of strings, optional): CLI args
- `filePatterns` (list of strings, optional): glob patterns to scope the server
- `enabled` (boolean, default `true`): whether the server starts on CLI launch

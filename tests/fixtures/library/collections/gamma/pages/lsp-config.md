---
title: "LSP configuration overview"
tags: [plugins, lsp, configuration]
---

The LSP block in `opencode.jsonc` configures one or more language servers. Each server has a `command`, optional `args`, optional `filePatterns`, and an `enabled` flag.

```jsonc
{
  "lsp": {
    "pyright": { "command": "pyright-langserver", "--stdio": true },
    "tsserver": { "command": "typescript-language-server", "--stdio": true }
  }
}
```

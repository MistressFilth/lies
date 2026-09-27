---
title: "Pyright for Python"
tags: [plugins, lsp, python, pyright]
---

Pyright is the recommended Python language server. Install via `pip install pyright` (the `pyright-langserver` binary ships with the package). Configure in `opencode.jsonc`:

```jsonc
{ "lsp": { "pyright": { "command": "pyright-langserver", "--stdio": true } } }
```

For monorepos with multiple Python interpreters, set `LSP_PYRIGHT_PYTHON_PATH` env var to the target interpreter.

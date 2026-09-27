---
title: "Server binary resolution"
tags: [plugins, lsp, server-binary]
---

The CLI resolves `command` against `$PATH` first. If `command` is an absolute path, that path is used as-is. Relative paths resolve against the CLI's `cwd`. Symlinks in the resolved path are followed.

For sandboxed installs, set `command` to an absolute path under `~/.local/bin/`.

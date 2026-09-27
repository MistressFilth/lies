---
title: "Server output formatters"
tags: [plugins, lsp, formatters]
---

LSP formatters run on every file save the server touches. Configure per-server via the `formatters` field adjacent to the LSP block, NOT inside the server entry itself.

If a server's diagnostics conflict with a formatter, disable the formatter for that language via `formatters.<lang>.disabled = true`.

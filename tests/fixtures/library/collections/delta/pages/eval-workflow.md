---
title: "Eval workflow overview"
tags: [plugins, eval, testing]
---

Plugin evals measure how reliably a plugin steers the agent toward correct behavior. The workflow: write cases → run → grade → compare to baseline → gate CI.

`plugin eval` is the orchestrator; `plugin eval init` scaffolds a new eval suite.

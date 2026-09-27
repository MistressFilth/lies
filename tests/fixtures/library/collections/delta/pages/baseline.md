---
title: "Baselines"
tags: [plugins, eval, baseline]
---

A baseline is the no-plugin reference score for a case suite. `plugin eval baseline save` records the current run as the baseline; `plugin eval --baseline <file>` compares against it.

Baselines are per-suite. Commit baselines alongside the suite so CI runs are reproducible.

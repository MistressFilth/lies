---
title: "CI integration"
tags: [plugins, eval, ci]
---

Gate CI on `plugin eval --threshold 0.9 --baseline main`. The `--threshold` flag fails the run if the median score across cases drops below the threshold. The `--baseline` flag compares against a git ref.

Run evals on every PR. Failures block merge.

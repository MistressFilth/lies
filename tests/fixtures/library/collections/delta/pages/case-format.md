---
title: "Case file format"
tags: [plugins, eval, format]
---

Each case is a directory under `evals/<suite>/<case>/` containing a `prompt.md` (the input), an optional `setup.sh` (runs before the prompt), and one or more grader files.

The case directory's name becomes the case identifier in the eval report.

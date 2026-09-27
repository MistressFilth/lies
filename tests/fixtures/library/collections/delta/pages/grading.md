---
title: "Graders"
tags: [plugins, eval, grading]
---

A grader is a pass/fail check on the agent's output. Built-in graders: `regex` (matches against the reply), `tool_called` (checks the agent invoked a tool), `rubric` (asks a second model to judge).

Combine graders with `all` / `any` / `min(n)` boolean operators per case.

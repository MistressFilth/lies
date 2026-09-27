---
title: "Hint scope predicates"
tags: [plugins, hints, scope]
---

Hint scope is a JSON predicate: `{"file_pattern": "*.py", "intent": "edit"}`. The CLI evaluates the predicate against the current session state at session start. Mismatches skip the hint silently.

Empty predicates always match. Use narrow predicates for noise reduction.

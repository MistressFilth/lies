---
title: Pydantic
type: concept
---

# Pydantic

## Definition

Pydantic is a Python data validation library that uses Python type
annotations to define and validate data models. Nested models are
validated recursively.

## When to Use

Use Pydantic when inner model state must conform to outer validation,
or when you want to serialise a structured Python object to JSON
without writing a hand-rolled encoder.

## Examples

```python
from pydantic import BaseModel

class Inner(BaseModel):
    n: int

class Outer(BaseModel):
    inner: Inner

Outer(inner={"n": 1})  # parses + validates recursively
```
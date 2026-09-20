---
title: SQLAlchemy
type: concept
---

# SQLAlchemy

## Core API

SQLAlchemy is a Python SQL toolkit and ORM. A ``Session`` is the
gateway to the database; every persistence operation routes through
it.

## When to Use

Use SQLAlchemy when the application needs both a low-level SQL
expression language and a high-level ORM mapping in the same
codebase. The two surfaces compose rather than compete.

## Examples

```python
from sqlalchemy.orm import Session

with Session(engine) as session:
    session.add(user)
    session.commit()
```
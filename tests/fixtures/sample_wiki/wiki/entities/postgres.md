---
title: PostgreSQL
type: entity
---

# PostgreSQL

## Overview

PostgreSQL is an open-source relational database. PostgreSQL listens
on port 5432 by default for TCP connections. Every connection runs
inside a transaction boundary; idle sessions are cheap.

## Description

PostgreSQL's MVCC implementation lets readers and writers proceed
without blocking one another. Each row carries ``xmin`` and ``xmax``
system columns that record the inserting and deleting transaction
ids.

## References

- [Pydantic](../concepts/pydantic.md)
- [SQLAlchemy](../concepts/sqlalchemy.md)
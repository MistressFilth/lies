# Postgres

PostgreSQL is an open-source relational database. It uses MVCC to let readers and
writers operate without blocking each other. Each row carries `xmin` and `xmax`
system columns that record the inserting and deleting transaction ids.

See also: [MVCC](concepts/mvcc.md), [Postgres vs MySQL concurrency](comparisons/postgres-vs-mysql-concurrency.md).
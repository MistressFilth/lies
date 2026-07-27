# Postgres MVCC

PostgreSQL uses Multi-Version Concurrency Control (MVCC) to allow readers
and writers to operate without blocking each other. Each row has xmin and
xmax system columns that track the inserting and deleting transactions.

This is in contrast to MySQL's InnoDB, which uses row-level locking with
undo logs to provide similar isolation guarantees.

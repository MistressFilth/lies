# MySQL InnoDB notes

InnoDB uses row-level locking combined with multi-versioning via undo logs.
Readers see a consistent snapshot at the time their transaction started.

This differs from PostgreSQL's approach, which stores multiple versions
in the table itself (heap) rather than in a separate undo log.

# Postgres vs MySQL concurrency

Postgres stores multiple versions of each row in the heap itself. MySQL's InnoDB
keeps the current row in the page and prior versions in an undo log. Both
provide read-committed isolation by default; only Postgres gives full
serializable isolation out of the box.

See also: [Postgres](entities/postgres.md), [MySQL](entities/mysql.md).
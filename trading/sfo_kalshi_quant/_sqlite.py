"""A SQLite connection whose ``with`` block also releases the handle.

``sqlite3.Connection.__exit__`` commits or rolls back but deliberately does NOT
close. Every ``with sqlite3.connect(...) as conn:`` in this package therefore
leaked one file descriptor per call. On a host with ``ulimit -n`` 256 a full test
run exhausted the table partway through and produced a shifting set of roughly
thirty failures that looked like flaky tests; long-lived producers leak just as
steadily, they simply have a larger table to exhaust.

Use :func:`connect` wherever the connection's lifetime is the ``with`` block.
Do NOT use it where a connection is bound to a name and then re-entered with a
nested ``with conn:`` transaction block -- closing on exit would end the
transaction early. Those sites manage their own lifetime and are left as-is.
"""

from __future__ import annotations

import sqlite3
from typing import Any


class ClosingConnection(sqlite3.Connection):
    """``sqlite3.Connection`` that closes when its ``with`` block exits."""

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    """``sqlite3.connect`` that hands back a self-closing connection."""

    kwargs.setdefault("factory", ClosingConnection)
    return sqlite3.connect(*args, **kwargs)

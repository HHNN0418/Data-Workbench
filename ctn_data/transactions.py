"""Short write transactions shared by imports and browser batch ingestion."""
from contextlib import contextmanager
from .db import begin_write, connect, initialize


@contextmanager
def write_connection(target, connection=None):
    if connection is not None:
        # The caller owns the transaction, including rollback and receipt commit.
        yield connection
        return
    initialize(target)
    conn = connect(target)
    try:
        begin_write(conn, 'official_imports')
        with conn:
            yield conn
    finally:
        conn.close()

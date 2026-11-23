"""Classify retryable storage failures without exposing database error text.

Only bookkeeping transactions may use this classification. Retrying a model
request, a platform action, or an arbitrary business callback is not safe.
"""
from __future__ import annotations

import re
import sqlite3


def _causes(error):
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def storage_error_code(error):
    for cause in _causes(error):
        code = getattr(cause, 'sqlstate', None)
        if isinstance(code, str) and re.fullmatch(r'[0-9A-Z]{5}', code):
            return code
    return ''


def is_transient_storage_error(error):
    code = storage_error_code(error)
    if code:
        return code.startswith('08') or code in {
            '55P03', '40001', '40P01', '57014', '53300', '57P01', '57P02', '57P03'}
    for cause in _causes(error):
        if isinstance(cause, sqlite3.OperationalError):
            message = str(cause).lower()
            if any(fragment in message for fragment in (
                    'database is locked', 'database table is locked',
                    'database schema is locked', 'database is busy')):
                return True
        if (type(cause).__module__.split('.')[0] == 'psycopg'
                and type(cause).__name__ in ('OperationalError', 'InterfaceError')):
            return True
    return False

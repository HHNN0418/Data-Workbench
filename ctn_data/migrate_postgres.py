"""Verified, transactional SQLite-to-PostgreSQL import and portable backups.

Migrate a consistent SQLite backup, never the live WAL file copied by hand.
The destination must be empty. Every source column, ID and value is preserved;
per-table canonical content digests must match before the import commits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .db import SCHEMA, SCHEMA_VERSION, connect, configure_sqlite, initialize, is_postgres, iter_rows, portable_indexes, utc_now
from .postgres import IDENTITY_TABLES, SCHEMA_FINGERPRINT_KEY

# Read models rebuilt by the target's triggers while the source rows are
# copied. Backups made by backup_postgres() carry SQLite's own copy.
DERIVED_TABLES = frozenset(('counter_sampling',))
# initialize() writes these keys into every new database.
INITIAL_META_KEYS = ('created_at', 'mode', SCHEMA_FINGERPRINT_KEY)


def identifier(name):
    if not re.fullmatch(r'[a-z_][a-z0-9_]*', name):
        raise ValueError('Invalid database identifier')
    return '"' + name + '"'


def schema_tables():
    return re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', SCHEMA)


def digest_rows(rows):
    digest, count = hashlib.sha256(), 0
    for row in rows:
        values = list(row)
        encoded = json.dumps(values, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
        count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def _columns(source, table):
    return [row[1] for row in source.execute(f'PRAGMA table_info({identifier(table)})')]


def _select(table, columns, connection):
    names = ','.join(identifier(name) for name in columns)
    # All tables have one stable primary key first. Explicit bytewise ordering
    # avoids server locale differences changing the digest for Unicode keys.
    collation = 'C' if getattr(connection, 'dialect', '') == 'postgresql' else 'BINARY'
    order_columns = [columns[0]]
    if table == 'agent_memory_links':
        order_columns.append('source_id')
    order = ','.join(f'CAST({identifier(name)} AS TEXT) COLLATE "{collation}"' for name in order_columns)
    return f'SELECT {names} FROM {identifier(table)} ORDER BY {order}'


def _copy(source, target, table, columns):
    names = ','.join(identifier(name) for name in columns)
    insert = f'INSERT INTO {identifier(table)}({names}) VALUES({",".join("?" for _ in columns)})'
    with iter_rows(source, _select(table, columns, source)) as cursor:
        while batch := cursor.fetchmany(500):
            target.executemany(insert, [tuple(row) for row in batch])


def _digest(connection, table, columns):
    with iter_rows(connection, _select(table, columns, connection)) as cursor:
        return digest_rows(cursor)


def migrate(source_path, target_dsn):
    if not is_postgres(target_dsn):
        raise ValueError('Target must be a PostgreSQL DSN')
    source_path = Path(source_path).resolve(strict=True)
    source = configure_sqlite(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True))
    source.row_factory = sqlite3.Row
    target = None
    report = {'source': str(source_path), 'started_at': utc_now(), 'tables': {}, 'verified': False}
    try:
        source.execute('BEGIN')
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Source database failed integrity check')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Source database has foreign key violations')
        if source.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
            raise ValueError('Source schema is newer than this application')
        present = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        unknown = present - set(schema_tables()) - DERIVED_TABLES
        if unknown:
            raise ValueError('Source has unrecognized tables; refusing incomplete migration: ' + ','.join(sorted(unknown)))
        initialize(target_dsn)
        target = connect(target_dsn)
        with target:
            target.execute('BEGIN IMMEDIATE')
            table_list = ','.join(identifier(table) for table in schema_tables())
            target.raw.execute(f'LOCK TABLE {table_list} IN ACCESS EXCLUSIVE MODE')
            for table in schema_tables():
                count = target.execute(f'SELECT COUNT(*) FROM {identifier(table)}').fetchone()[0]
                if table == 'meta':
                    extra = target.execute('SELECT COUNT(*) FROM meta WHERE key NOT IN (' +
                                           ','.join('?' for _ in INITIAL_META_KEYS) + ')', INITIAL_META_KEYS).fetchone()[0]
                    if extra:
                        raise ValueError('Target database is not empty')
                elif count:
                    raise ValueError('Target database is not empty: ' + table)
            target.execute('DELETE FROM meta')
            for table in schema_tables():
                if table not in present:
                    continue
                columns = _columns(source, table)
                _copy(source, target, table, columns)
                source_digest = _digest(source, table, columns)
                target_digest = _digest(target, table, columns)
                if source_digest != target_digest:
                    raise RuntimeError('Migration content verification failed: ' + table)
                report['tables'][table] = source_digest
            for table in sorted(IDENTITY_TABLES):
                maximum = target.execute(f'SELECT MAX(id) FROM {identifier(table)}').fetchone()[0]
                target.raw.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)',
                                   (table, 'id', maximum if maximum is not None else 1, maximum is not None))
            report['verified'] = True
        report['finished_at'] = utc_now()
        return report
    finally:
        source.close()
        if target:
            target.close()


def backup_postgres(dsn, directory):
    """Export a consistent portable SQLite snapshot, with checked FK and hashes."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace('-', '').replace(':', '').replace('.', '')
    destination = directory / f'ctn-pg-{stamp}.sqlite3'
    temporary = destination.with_suffix('.partial')
    initialize(temporary)
    source, target = connect(dsn), connect(temporary)
    try:
        source.execute('BEGIN')  # Adapter maps this to REPEATABLE READ.
        with target:
            target.execute('DELETE FROM meta')
            for table in schema_tables():
                columns = _columns(target, table)
                _copy(source, target, table, columns)
                if _digest(source, table, columns) != _digest(target, table, columns):
                    raise RuntimeError('Backup content verification failed: ' + table)
            if target.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('Backup foreign key verification failed')
            portable_indexes(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup integrity verification failed')
        target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        target.execute('PRAGMA journal_mode=DELETE')
    except Exception:
        target.close()
        source.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
        temporary.replace(destination)
        return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--target', help='PostgreSQL DSN; prefer --target-file to avoid shell history')
    group.add_argument('--target-file', help='Local file containing PostgreSQL DSN')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    from .secret_files import read_secret
    target = read_secret(Path(args.target_file)).strip() if args.target_file else args.target
    report = migrate(args.source, target)
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'verified': report['verified'], 'tables': len(report['tables']),
                      'rows': sum(item['rows'] for item in report['tables'].values()), 'report': str(output)}))


if __name__ == '__main__':
    main()

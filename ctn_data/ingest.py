"""Validated, historical ingestion. Public observations never imply ownership."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid

from .db import raw_record, timestamp_key, utc_now


def observed_time(value=None):
    if value is None:
        return utc_now()
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('observation time must include a UTC offset')
    return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _id(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{label} must be a positive integer')
    return value


@contextmanager
def _atomic(conn):
    name = 'ingest_' + uuid.uuid4().hex
    conn.execute('SAVEPOINT ' + name)
    try:
        yield
        conn.execute('RELEASE SAVEPOINT ' + name)
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT ' + name)
        conn.execute('RELEASE SAVEPOINT ' + name)
        raise


def _existing(conn, table, column, key):
    row = conn.execute(f'SELECT * FROM {table} WHERE {column}=?', (key,)).fetchone()
    return dict(row) if row is not None else {}


def _save_posts_postgres(conn, items, source, at, cursor):
    """Pipeline independent writes while retaining sequential merge semantics."""
    fields = ('author_id', 'kind', 'body', 'reply_to_post_id', 'root_post_id',
              'repost_of_post_id', 'visibility', 'created_at', 'edited_at')
    with _atomic(conn):
        post_ids = set()
        for post in items:
            if not isinstance(post, dict):
                raise ValueError('post must be an object')
            post_ids.add(_id(post.get('post_id'), 'post_id'))
        if items:
            post_ids = sorted(post_ids)
            # One pipeline, in the same ID order used by every post writer.
            # Executemany executes its parameter sets sequentially on one PG session.
            locks = [(int.from_bytes(hashlib.sha256(f'ctn:post:{post_id}'.encode()).digest()[:8],
                                     signed=True),) for post_id in post_ids]
            conn.executemany('SELECT pg_advisory_xact_lock(?)', locks)
            existing = {row['post_id']:dict(row) for row in conn.execute(
                'SELECT *,ctn_timestamp(last_seen) AS ingest_last_seen_key FROM posts WHERE post_id=ANY(?)',
                (post_ids,)).fetchall()}
            raw_source = 'social:' + source
            raw_rows, prepared = [], []
            observed_key = timestamp_key(at)
            for post in items:
                post_id = post['post_id']
                old = existing.get(post_id, {})
                values = {field: post.get(field, old.get(field)) for field in fields}
                if values['author_id'] is not None:
                    _id(values['author_id'], 'author_id')
                for field in ('reply_to_post_id', 'root_post_id', 'repost_of_post_id'):
                    if values[field] is not None:
                        _id(values[field], field)
                embed = _json(post['embed']) if 'embed' in post and post['embed'] is not None else (
                    None if 'embed' in post else old.get('embed_json'))
                endpoint = f'/api/posts/{post_id}'
                serialized = _json(post)
                digest = hashlib.sha256((raw_source+'\n'+endpoint+'\n'+serialized).encode()).hexdigest()
                raw_rows.append((digest,raw_source,endpoint,at,serialized))
                row = (post_id, *[values[field] for field in fields[:7]], embed,
                       values['created_at'], values['edited_at'], old.get('first_seen', at), at)
                content_hash = hashlib.sha256(_json({'body': values['body'],
                    'visibility': values['visibility'], 'embed_json': embed}).encode('utf-8')).hexdigest()
                prepared.append((row,digest,(post_id,values['body'],values['edited_at'],at,content_hash),
                                 (post_id,source,at,post.get('rank_position'),post.get('global_hot_score'))))
                # Repeated IDs must merge with the preceding *accepted* input.
                # Older observations still append history but never become merge state.
                old_key = old.get('ingest_last_seen_key')
                if not old or (old_key is not None and observed_key >= old_key):
                    existing[post_id] = {**values,'embed_json':embed,'first_seen':old.get('first_seen',at),
                                         'last_seen':at,'ingest_last_seen_key':observed_key}
            digests = list(dict.fromkeys(row[0] for row in raw_rows))
            # Hold reused orphan payloads until their new post/version links
            # commit. Housekeeping skips these locks instead of deleting IDs
            # between deduplication and the foreign-key insert.
            conn.execute('SELECT id FROM raw_records WHERE sha256=ANY(?) '
                         'ORDER BY sha256 FOR KEY SHARE', (digests,)).fetchall()
            conn.executemany('''INSERT OR IGNORE INTO raw_records
                (sha256,source,endpoint,observed_at,payload_json) VALUES(?,?,?,?,?)''',raw_rows)
            raw_ids = {row['sha256']:row['id'] for row in conn.execute(
                'SELECT sha256,id FROM raw_records WHERE sha256=ANY(?)',
                (digests,)).fetchall()}
            conn.executemany('''INSERT INTO posts
                (post_id,author_id,kind,body,reply_to_post_id,root_post_id,repost_of_post_id,
                 visibility,embed_json,created_at,edited_at,first_seen,last_seen,raw_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
                author_id=excluded.author_id,kind=excluded.kind,body=excluded.body,
                reply_to_post_id=excluded.reply_to_post_id,root_post_id=excluded.root_post_id,
                repost_of_post_id=excluded.repost_of_post_id,visibility=excluded.visibility,
                embed_json=excluded.embed_json,created_at=excluded.created_at,
                edited_at=excluded.edited_at,last_seen=excluded.last_seen,raw_id=excluded.raw_id
                WHERE ctn_timestamp(excluded.last_seen)>=ctn_timestamp(posts.last_seen)''',
                [(*row,raw_ids[digest]) for row,digest,_,_ in prepared])
            conn.executemany('''INSERT OR IGNORE INTO post_versions
                (post_id,body,edited_at,observed_at,content_hash,raw_id) VALUES (?,?,?,?,?,?)''',
                [(*version,raw_ids[digest]) for _,digest,version,_ in prepared])
            conn.executemany('''INSERT OR IGNORE INTO feed_observations
                (post_id,source,observed_at,rank_position,global_hot_score) VALUES (?,?,?,?,?)''',
                [feed for _,_,_,feed in prepared])
        if cursor is not None:
            conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                ('feed:' + source + ':cursor', str(cursor), at))
    return len(items)


def save_posts(conn, posts, source, observed_at=None, cursor=None):
    """Upsert public posts and append content versions/feed observations atomically."""
    at = observed_time(observed_at)
    items = list(posts)
    if getattr(conn, 'dialect', None) == 'postgresql':
        return _save_posts_postgres(conn, items, source, at, cursor)
    fields = ('author_id', 'kind', 'body', 'reply_to_post_id', 'root_post_id',
              'repost_of_post_id', 'visibility', 'created_at', 'edited_at')
    with _atomic(conn):
        for post in items:
            if not isinstance(post, dict):
                raise ValueError('post must be an object')
            post_id = _id(post.get('post_id'), 'post_id')
            old = _existing(conn, 'posts', 'post_id', post_id)
            values = {field: post.get(field, old.get(field)) for field in fields}
            if values['author_id'] is not None:
                _id(values['author_id'], 'author_id')
            for field in ('reply_to_post_id', 'root_post_id', 'repost_of_post_id'):
                if values[field] is not None:
                    _id(values[field], field)
            embed = _json(post['embed']) if 'embed' in post and post['embed'] is not None else (
                None if 'embed' in post else old.get('embed_json'))
            raw_id = raw_record(conn, 'social:' + source, f'/api/posts/{post_id}', post, at)
            row = (post_id, *[values[field] for field in fields[:7]], embed,
                   values['created_at'], values['edited_at'], old.get('first_seen', at), at, raw_id)
            conn.execute('''INSERT INTO posts
                (post_id,author_id,kind,body,reply_to_post_id,root_post_id,repost_of_post_id,
                 visibility,embed_json,created_at,edited_at,first_seen,last_seen,raw_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
                author_id=excluded.author_id,kind=excluded.kind,body=excluded.body,
                reply_to_post_id=excluded.reply_to_post_id,root_post_id=excluded.root_post_id,
                repost_of_post_id=excluded.repost_of_post_id,visibility=excluded.visibility,
                embed_json=excluded.embed_json,created_at=excluded.created_at,
                edited_at=excluded.edited_at,last_seen=excluded.last_seen,raw_id=excluded.raw_id
                WHERE ctn_timestamp(excluded.last_seen)>=ctn_timestamp(posts.last_seen)''', row)
            content_hash = hashlib.sha256(_json({'body': values['body'],
                'visibility': values['visibility'], 'embed_json': embed}).encode('utf-8')).hexdigest()
            conn.execute('''INSERT OR IGNORE INTO post_versions
                (post_id,body,edited_at,observed_at,content_hash,raw_id) VALUES (?,?,?,?,?,?)''',
                (post_id, values['body'], values['edited_at'], at, content_hash, raw_id))
            conn.execute('''INSERT OR IGNORE INTO feed_observations
                (post_id,source,observed_at,rank_position,global_hot_score) VALUES (?,?,?,?,?)''',
                (post_id, source, at, post.get('rank_position'), post.get('global_hot_score')))
        if cursor is not None:
            conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                ('feed:' + source + ':cursor', str(cursor), at))
    return len(items)


def save_counters(conn, post_id, payload, observed_at=None, source='social:counters'):
    """Append an observation. Missing values remain NULL, never fabricated zeroes."""
    post_id = _id(post_id, 'post_id')
    if not isinstance(payload, dict):
        raise ValueError('counters must be an object')
    if payload.get('post_id', post_id) != post_id:
        raise ValueError('counter post_id does not match requested post')
    counts = []
    for name in ('like_count', 'reply_count', 'repost_count', 'view_count'):
        value = payload.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(name + ' must be a nonnegative integer or null')
        counts.append(value)
    at = observed_time(observed_at)
    with _atomic(conn):
        raw_id = raw_record(conn, source, f'/api/posts/{post_id}/counters', payload, at)
        result = conn.execute('''INSERT OR IGNORE INTO counter_snapshots
            (post_id,observed_at,like_count,reply_count,repost_count,view_count,
             score_inputs_updated_at,raw_id) VALUES (?,?,?,?,?,?,?,?)''',
            (post_id, at, *counts, payload.get('score_inputs_updated_at'), raw_id))
        if result.rowcount == 0:
            existing = conn.execute('SELECT * FROM counter_snapshots WHERE post_id=? AND observed_at=?', (post_id, at)).fetchone()
            names = ('like_count','reply_count','repost_count','view_count','score_inputs_updated_at')
            incoming = (*counts, payload.get('score_inputs_updated_at'))
            if existing and tuple(existing[name] for name in names) != incoming:
                fingerprint = hashlib.sha256(_json([post_id,at,source,*incoming]).encode()).hexdigest()
                conn.execute('''INSERT OR IGNORE INTO counter_conflicts
                    (post_id,observed_at,source,observation_hash,like_count,reply_count,repost_count,
                     view_count,score_inputs_updated_at,raw_id,canonical_snapshot_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (post_id,at,source,fingerprint,*incoming,raw_id,existing['id']))
    return result.rowcount


def save_accounts(conn, accounts, observed_at=None):
    at = observed_time(observed_at)
    items = list(accounts)
    with _atomic(conn):
        for account in items:
            if not isinstance(account, dict):
                raise ValueError('account must be an object')
            account_id = _id(account.get('account_id'), 'account_id')
            old = _existing(conn, 'accounts', 'account_id', account_id)
            values = {key: account.get(key, old.get(key)) for key in
                      ('username', 'display_name', 'verified', 'private', 'created_at')}
            for key in ('verified', 'private'):
                if values[key] is not None:
                    if values[key] not in (True, False, 0, 1):
                        raise ValueError(key + ' must be boolean')
                    values[key] = int(values[key])
            raw_id = raw_record(conn, 'social:accounts', f'/api/accounts/{account_id}', account, at)
            conn.execute('''INSERT INTO accounts
                (account_id,username,display_name,verified,private,created_at,first_seen,last_seen,raw_id)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
                username=excluded.username,display_name=excluded.display_name,
                verified=excluded.verified,private=excluded.private,
                created_at=excluded.created_at,last_seen=excluded.last_seen,raw_id=excluded.raw_id''',
                (account_id, values['username'], values['display_name'], values['verified'],
                 values['private'], values['created_at'], old.get('first_seen', at), at, raw_id))
    return len(items)


def save_news(conn, articles, observed_at=None):
    at = observed_time(observed_at)
    items = list(articles)
    with _atomic(conn):
        for article in items:
            if not isinstance(article, dict):
                raise ValueError('article must be an object')
            site = article.get('site')
            if isinstance(site, dict):
                site = site.get('sub_url') or site.get('id')
            slug = article.get('slug')
            if not isinstance(site, str) or not site or not isinstance(slug, str) or not slug:
                raise ValueError('article requires site and slug')
            key = site + '/' + slug
            old = _existing(conn, 'news_articles', 'article_key', key)
            author = article.get('author', old.get('author'))
            if isinstance(author, (dict, list)):
                author = _json(author)
            raw_id = raw_record(conn, 'news', f'/{site}/articles/{slug}.json', article, at)
            conn.execute('''INSERT INTO news_articles
                (article_key,site,slug,title,body,author,published_at,first_seen,last_seen,raw_id)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(article_key) DO UPDATE SET
                title=excluded.title,body=excluded.body,author=excluded.author,
                published_at=excluded.published_at,last_seen=excluded.last_seen,raw_id=excluded.raw_id''',
                (key, site, slug, article.get('title', old.get('title')),
                 article.get('body', old.get('body')), author,
                 article.get('published_at', old.get('published_at')),
                 old.get('first_seen', at), at, raw_id))
    return len(items)

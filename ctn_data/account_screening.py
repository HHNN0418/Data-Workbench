"""Interpretable public-behavior triage; scores are uncalibrated review priorities."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import re
from statistics import mean, pstdev, median
import unicodedata

from .ai_evidence import _scope
from .db import connect, timestamp_key, utc_now

MAX_POSTS = 20000
MAX_BODY = 8000
VERSION = "public-behavior-heuristic-v1"
MIN_ORIGINAL_POSTS = 5
MIN_TIMESTAMPS = 8
MIN_DUPLICATE_LENGTH = 40
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
JOINS = (" FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id "
         "LEFT JOIN team_accounts t ON t.account_id=p.author_id")


def _where(scope):
    clauses = ["p.author_id IS NOT NULL", "(p.visibility IS NULL OR LOWER(p.visibility) IN ('','public'))",
               "(a.private IS NULL OR a.private=0)", "(t.is_own IS NULL OR t.is_own=0)"]
    args = []
    if scope['own'] == 'own':
        clauses.append('1=0')
    elif scope['own'] == 'unknown':
        clauses.append('t.team_label IS NULL')
    if scope['author_id'] is not None:
        clauses.append('p.author_id=?')
        args.append(scope['author_id'])
    for key, operator in [('since', '>='), ('until', '<=')]:
        if scope[key]:
            clauses.append(f'ctn_timestamp(p.created_at){operator}?')
            args.append(scope[key])
    return ' WHERE ' + ' AND '.join(clauses), args


def _original(row):
    kind = (row['kind'] or '').lower()
    # Explicit reshares/quoted reshares carry another author's content. Their
    # repetition must not become an originality or coordination accusation.
    return (row['repost_of_post_id'] is None and row['reply_to_post_id'] is None
            and not any(word in kind for word in ('repost', 'retweet', 'quote', 'reshare', 'reply')))


def _signature(text, full_length):
    if full_length > MAX_BODY:
        return None  # A shared prefix cannot establish identical full content.
    normalized = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip().casefold()
    if len(normalized) < MIN_DUPLICATE_LENGTH:
        return None
    return hashlib.sha256(normalized.encode()).hexdigest()


def _coordinated(events):
    """Mark events in a <=120-second window containing >=3 distinct accounts.

    Difference arrays mark qualifying ranges without repeatedly scanning dense
    windows, keeping total work linear after sorting each text signature.
    """
    by_account = defaultdict(lambda: {'post_ids': set(), 'signatures': set(), 'coordination_examples': []})
    for signature, rows in events.items():
        if len({row[1] for row in rows}) < 3:
            continue
        rows.sort()
        active_posts, start = {}, 0
        marks = [0] * (len(rows) + 1)
        for end, (at, author, post_id) in enumerate(rows):
            active_posts.setdefault(author, deque()).append((at, post_id))
            while rows[start][0] < at - 120:
                expired_author = rows[start][1]
                active_posts[expired_author].popleft()
                if not active_posts[expired_author]:
                    del active_posts[expired_author]
                start += 1
            if len(active_posts) >= 3:
                marks[start] += 1
                marks[end + 1] -= 1
                # A constant-size witness uses actual posts inside this window,
                # never all authors who happened to share a text on other days.
                witness_authors = [author]
                for peer_author in active_posts:
                    if peer_author != author:
                        witness_authors.append(peer_author)
                    if len(witness_authors) == 3:
                        break
                witness = [(candidate, *active_posts[candidate][0]) for candidate in witness_authors]
                first = min(item[1] for item in witness)
                last = max(item[1] for item in witness)
                for subject, _, subject_post_id in witness:
                    examples = by_account[subject]['coordination_examples']
                    if len(examples) >= 2 or any(item['text_signature'] == signature for item in examples):
                        continue
                    peers = [item for item in witness if item[0] != subject]
                    examples.append({'text_signature': signature, 'post_id': subject_post_id,
                        'window_start': (EPOCH + timedelta(seconds=first)).isoformat(),
                        'window_end': (EPOCH + timedelta(seconds=last)).isoformat(),
                        'peer_account_ids': [item[0] for item in peers],
                        'peer_post_ids': [item[2] for item in peers]})
        active = 0
        for index, (_, author, post_id) in enumerate(rows):
            active += marks[index]
            if active:
                by_account[author]['post_ids'].add(post_id)
                by_account[author]['signatures'].add(signature)
    return by_account


def _signal(key, title, points, maximum, measured, post_ids):
    if key == 'repeated_original_text':
        description = f"{measured['eligible_long_posts']}篇≥40字原文，首篇以外重复{measured['duplicate_posts_after_first']}篇（{measured['duplicate_ratio']:.1%}）"
    elif key == 'regular_publication_intervals':
        cv = measured['interval_cv']
        description = f"{measured['timestamp_posts']}个有效原帖时间，中位间隔{measured['median_interval_seconds']}秒，间隔变异系数{cv if cv is not None else '未知'}"
    elif key == 'publication_bursts':
        description = f"任意60秒内最多{measured['maximum_posts_in_60_seconds']}篇原帖"
    elif key == 'cross_account_exact_text':
        description = f"120秒内≥3账号完全同文：本账号涉及{measured['coordinated_posts']}篇、{measured['distinct_shared_texts']}种长正文"
    else:
        description = f"{measured['active_utc_days']}个UTC活跃日，其中{measured['days_with_at_least_18_active_hours']}日覆盖≥18个小时段"
    return {'key': key, 'title': title, 'points': points, 'max_points': maximum,
            'measured': description, 'measurements': measured, 'post_ids': list(dict.fromkeys(post_ids))[:8]}


def _signals(account, coordination):
    originals, times = account['originals'], sorted(account['times'])
    signatures = defaultdict(list)
    for post in originals:
        if post['signature']:
            signatures[post['signature']].append(post['post_id'])
    eligible_n = sum(len(ids) for ids in signatures.values())
    duplicates = sum(max(0, len(ids) - 1) for ids in signatures.values())
    duplicate_ratio = duplicates / eligible_n if eligible_n else 0
    repeat_points = 30 if duplicates >= 3 and duplicate_ratio >= .6 else 15 if duplicates >= 2 and duplicate_ratio >= .35 else 0
    repeated_ids = [post_id for ids in signatures.values() if len(ids) > 1 for post_id in ids]
    intervals = [times[index][0] - times[index - 1][0] for index in range(1, len(times))]
    avg = mean(intervals) if intervals else 0
    cv = pstdev(intervals) / avg if avg > 0 else None
    span = times[-1][0] - times[0][0] if times else 0
    typical = median(intervals) if intervals else None
    regular = len(times) >= 8 and span >= 3600 and typical >= 60 and cv is not None and cv <= .08
    regular_points = 20 if regular and len(times) >= 12 and cv <= .05 else 10 if regular else 0
    left, max_burst, burst_ids = 0, 0, []
    for right, (at, _) in enumerate(times):
        while times[left][0] < at - 60:
            left += 1
        size = right - left + 1
        if size > max_burst:
            max_burst = size
            burst_ids = [post_id for _, post_id in times[left:min(right + 1, left + 8)]]
    burst_points = 20 if max_burst >= 10 else 10 if max_burst >= 6 else 0
    coordinated_ids = sorted(coordination.get('post_ids', []))
    shared_texts = len(coordination.get('signatures', []))
    coordination_examples = coordination.get('coordination_examples', [])[:2]
    peer_account_ids = list(dict.fromkeys(peer for example in coordination_examples
                                         for peer in example['peer_account_ids']))[:4]
    peer_post_ids = list(dict.fromkeys(peer for example in coordination_examples
                                      for peer in example['peer_post_ids']))[:4]
    coordination_points = 20 if len(coordinated_ids) >= 3 and shared_texts >= 2 else 10 if coordinated_ids else 0
    hours = defaultdict(set)
    for at, _ in times:
        value = EPOCH + timedelta(seconds=at)
        hours[value.date().isoformat()].add(value.hour)
    broad_days = sum(len(hour_set) >= 18 for hour_set in hours.values())
    continuous_points = 10 if broad_days >= 3 and span >= 48 * 3600 else 0
    return [
        _signal('repeated_original_text', '长正文重复', repeat_points, 30,
                {'eligible_long_posts': eligible_n, 'duplicate_posts_after_first': duplicates,
                 'duplicate_ratio': round(duplicate_ratio, 4), 'minimum_text_characters': MIN_DUPLICATE_LENGTH}, repeated_ids),
        _signal('regular_publication_intervals', '发帖间隔过于规律', regular_points, 20,
                {'timestamp_posts': len(times), 'interval_cv': round(cv, 4) if cv is not None else None,
                 'median_interval_seconds': typical, 'required_span_seconds': 3600},
                [post_id for _, post_id in times] if regular_points else []),
        _signal('publication_bursts', '短时间密集发帖', burst_points, 20,
                {'maximum_posts_in_60_seconds': max_burst, 'window_seconds': 60}, burst_ids if burst_points else []),
        _signal('cross_account_exact_text', '跨账号同文同步', coordination_points, 20,
                {'coordinated_posts': len(coordinated_ids), 'distinct_shared_texts': shared_texts,
                 'minimum_distinct_accounts': 3, 'window_seconds': 120,
                 'peer_account_ids': peer_account_ids, 'peer_post_ids': peer_post_ids,
                 'coordination_examples': coordination_examples}, coordinated_ids),
        _signal('continuous_daily_activity', '跨日长时段活跃', continuous_points, 10,
                {'active_utc_days': len(hours), 'days_with_at_least_18_active_hours': broad_days,
                 'required_days': 3, 'time_basis': 'publication_time_utc'},
                [post_id for _, post_id in times] if continuous_points else []),
    ]


def build_screening(db_path, params=None, limit=100, min_score=0, *, include_diagnostics=False):
    """Read bounded public observations; never submit reports or call a model."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit 必须为 1–200 的整数')
    if type(min_score) is not int or not 0 <= min_score <= 100:
        raise ValueError('min_score 必须为 0–100 的整数')
    scope = _scope(params)
    where, args = _where(scope)
    accounts, events = {}, defaultdict(list)
    with closing(connect(db_path)) as conn:
        conn.execute('BEGIN')
        source_count = conn.execute('SELECT COUNT(*)' + JOINS + where, args).fetchone()[0]
        source_accounts = conn.execute('SELECT COUNT(DISTINCT p.author_id)' + JOINS + where, args).fetchone()[0]
        rows = conn.execute('''SELECT p.post_id,p.author_id,p.kind,p.reply_to_post_id,p.repost_of_post_id,
            SUBSTR(COALESCE(p.body,''),1,8000) AS body,LENGTH(COALESCE(p.body,'')) AS body_length,
            p.created_at,a.username,a.verified''' + JOINS + where +
            ' ORDER BY ctn_timestamp(p.created_at) DESC NULLS LAST,p.post_id DESC LIMIT ?', args + [MAX_POSTS])
        scanned = 0
        for row in rows:
            scanned += 1
            author = row['author_id']
            account = accounts.setdefault(author, {'account_id': author,
                'username': (row['username'] or '')[:100] or None, 'verified': row['verified'],
                'posts': 0, 'originals': [], 'times': [], 'examples': []})
            account['posts'] += 1
            if not _original(row) or not row['body'].strip():
                continue
            key = timestamp_key(row['created_at'])
            at = datetime.fromisoformat(key.replace('Z', '+00:00')).timestamp() if key else None
            signature = _signature(row['body'], row['body_length'])
            post = {'post_id': row['post_id'], 'signature': signature}
            account['originals'].append(post)
            if len(account['examples']) < 3:
                account['examples'].append({'post_id': row['post_id'], 'text': row['body'][:500], 'created_at': key})
            if at is not None:
                account['times'].append((at, row['post_id']))
                if signature:
                    events[signature].append((at, author, row['post_id']))
        # Keep full known counts for inspected authors without retaining an
        # unbounded map of all authors in the wider source scope.
        for row in conn.execute('SELECT p.author_id,COUNT(*) AS n' + JOINS + where + ' GROUP BY p.author_id', args):
            if row['author_id'] in accounts:
                accounts[row['author_id']]['known_post_count'] = row['n']
    coordination = _coordinated(events)
    items = []
    insufficient_count = 0
    scored_count = signaled_count = 0
    for account in accounts.values():
        times = sorted(account['times'])
        enough = len(account['originals']) >= MIN_ORIGINAL_POSTS and len(times) >= MIN_TIMESTAMPS
        signals = _signals(account, coordination.get(account['account_id'], {}))
        score = sum(signal['points'] for signal in signals) if enough else None
        if not enough:
            insufficient_count += 1
        else:
            scored_count += 1
            signaled_count += int(score > 0)
        if min_score and (score is None or score < min_score):
            continue
        first = (EPOCH + timedelta(seconds=times[0][0])).isoformat() if times else None
        last = (EPOCH + timedelta(seconds=times[-1][0])).isoformat() if times else None
        items.append({'account_id': account['account_id'], 'username': account['username'], 'verified': account['verified'],
            'known_post_count': account['known_post_count'], 'risk_priority_score': score, 'score': score,
            'band': '资料不足' if score is None else '优先复核' if score >= 60 else '建议复核' if score >= 30 else '暂未触发明显信号',
            'data_observed': {'posts': account['posts'], 'timestamps': len(times), 'original_text_posts': len(account['originals']),
                'observed_span_hours': round((times[-1][0] - times[0][0]) / 3600, 3) if times else None,
                'first_published_at': first, 'last_published_at': last,
                'timestamp_basis': 'original_post_created_at', 'sufficient': enough},
            'signals': signals, 'examples': account['examples'],
            'account_report_eligibility': {'status': 'unknown', 'permanently_hidden_posts': None, 'official_action_count': None,
                'reason': '缺少官方永久隐藏帖子数与官方行为总量，无法确认账号举报门槛；公开帖子数不替代官方计数。'}})
    items.sort(key=lambda item: (item['score'] is not None, item['score'] if item['score'] is not None else -1,
                                item['known_post_count'], item['account_id']), reverse=True)
    matched_count = len(items)
    exported = items[:limit]
    if include_diagnostics:
        # The model should also inspect zero/unknown-rule cases beyond the top
        # page. These are coverage diagnostics, not negative truth labels.
        reserve = max(1, limit // 6)
        diagnostics = ([x for x in items if x['score'] == 0][:reserve] +
                       [x for x in items if x['score'] is None][:reserve])
        diagnostic_ids = {x['account_id'] for x in diagnostics}
        priority = [x for x in items if x['account_id'] not in diagnostic_ids]
        exported = (priority[:max(0, limit - len(diagnostics))] + diagnostics)[:limit]
    return {'items': exported, 'summary': {'source_post_count': source_count, 'source_account_count': source_accounts,
        'scanned_post_count': scanned, 'scanned_account_count': len(accounts), 'matched_account_count': matched_count,
        'returned_count': min(limit, matched_count), 'insufficient_account_count': insufficient_count,
        'scored_account_count': scored_count, 'signaled_account_count': signaled_count,
        'post_limit': MAX_POSTS, 'truncated': source_count > scanned, 'scope': scope,
        'reason': 'own_scope_excluded' if scope['own'] == 'own' else None, 'excluded_own_accounts': True,
        'min_score': min_score}, 'methodology': {'version': VERSION, 'score_name': '人工复核优先分',
        'range': [0, 100], 'calibrated': False, 'bot_probability': False,
        'minimum_original_text_posts': MIN_ORIGINAL_POSTS, 'minimum_valid_publication_times': MIN_TIMESTAMPS,
        'thresholds': {'repeated_text': '≥40字，重复率≥60%且重复≥3加30；≥35%且≥2加15',
            'regular_intervals': '≥8原帖时间、跨度≥1小时、中位间隔≥60秒、CV≤0.08加10；≥12且CV≤0.05加20',
            'bursts': '60秒内≥6原帖加10，≥10加20',
            'coordination': '120秒内≥3账号完全同文加10；本账号≥3相关帖且≥2种同文加20',
            'continuous_activity': '≥3个UTC日每天覆盖≥18小时且跨度≥48小时加10'}},
        'caveats': ['分数是未校准的工程启发式复核排序，不是机器人概率、违规结论或官方举报资格。',
            '来源只含已采集公开数据；至少5篇原文、8个有效原帖发布时间才给分。缺失资料显示未知，不按低风险处理。',
            '已排除本队注册账号与明确私密内容；转帖、引用转帖、回复不参与原创重复、同步和时间行为评分。',
            '新闻编辑室、定时发布和真实用户复制消息也可能触发信号；verified 不等于NPC标签，AI文风不作检测依据。',
            f'最多检查最近{MAX_POSTS}篇公开帖，每篇最多{MAX_BODY}字；过长正文不用于完全同文判断。时间信号使用发布时间，不使用采集时间。',
            '账号举报要求的官方永久隐藏帖子与官方行为次数尚未接入；候选帖子编号仅供复核，不代表可举报。'],
        'generated_at': utc_now()}

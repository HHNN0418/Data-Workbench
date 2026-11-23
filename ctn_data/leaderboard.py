"""Competition leaderboard snapshots (D104), read-only.

The paired extension reads https://community.capturethenarrative.com/user/competitions/2 from our
team page every 10–15 minutes and sends the leaderboard <table> as HTML (kind=leaderboard). This
module parses it, so a change of page layout needs a change here only. Rows the extension parsed
itself are a fallback; a table neither side can read is kept in meta for fixing the parser.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from .db import connect, timestamp_key, utc_now

COMPETITION_ID = 2
OWN_TEAM = 'Agent Zero'
WINDOWS = (1, 6, 24)
MAX_TABLE_BYTES = 300 * 1024
MAX_TEAMS = 500
MAX_SERIES_TEAMS = 12
MAX_SERIES_POINTS = 400
FAILURE_KEY = 'leaderboard_parse_failure'
# Table leaderboard_snapshots lives in db.SCHEMA.

_NUMBER = re.compile(r'[-+]?\d+(?:\.\d+)?')
_SCORE = {'score', 'totalscore', 'total', 'points', 'totalpoints', 'pts', '得分', '积分', '分数', '总分'}
_TEAM = {'team', 'teamname', 'name', '队伍', '队名', '队伍名称'}
_RANK = {'#', 'rank', 'pos', 'position', 'place', 'no', '排名', '名次'}
_RANK_CELL = re.compile(r'\D{0,3}\d+\D{0,3}')


class _Table(HTMLParser):
    """Rows of the outermost <table>; text of nested tables, scripts and styles is skipped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = self.skip = 0
        self.closed = False
        self.rows, self.row, self.cell, self.section = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if self.closed:
            return
        if tag == 'table':
            self.depth += 1
        elif tag in ('script', 'style'):
            self.skip += 1
        if self.depth != 1 or tag == 'table':
            return
        attrs = dict(attrs)
        if tag in ('thead', 'tbody', 'tfoot'):
            self._end_row()
            self.section = tag
        elif tag == 'tr':
            self._end_row()
            self.row = {'section': self.section, 'cells': []}
        elif tag in ('td', 'th'):
            self._end_cell()
            if self.row is None:
                self.row = {'section': self.section, 'cells': []}
            span = attrs.get('colspan') or '1'
            self.cell = {'header': tag == 'th', 'text': [], 'team_id': None,
                         'span': max(1, min(int(span), 20)) if span.isdigit() else 1}
        elif tag == 'a' and self.cell is not None and self.cell['team_id'] is None:
            match = re.search(r'/teams/(\d+)', attrs.get('href') or '')
            if match:
                self.cell['team_id'] = int(match.group(1))
        elif tag == 'br' and self.cell is not None:
            self.cell['text'].append(' ')

    def handle_endtag(self, tag):
        if self.closed:
            return
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag == 'table':
            if self.depth == 1:
                self._end_row()
                self.closed = True
            self.depth = max(0, self.depth - 1)
        elif self.depth == 1:
            if tag in ('td', 'th'):
                self._end_cell()
            elif tag == 'tr':
                self._end_row()
            elif tag in ('thead', 'tbody', 'tfoot'):
                self._end_row()
                self.section = None

    def handle_data(self, data):
        if self.depth == 1 and not self.skip and not self.closed and self.cell is not None:
            self.cell['text'].append(data)

    def _end_cell(self):
        if self.cell is not None and self.row is not None:
            self.cell['text'] = ' '.join(''.join(self.cell['text']).split())
            self.row['cells'].append(self.cell)
        self.cell = None

    def _end_row(self):
        self._end_cell()
        if self.row is not None and self.row['cells']:
            self.rows.append(self.row)
        self.row = None


def _key(text):
    return re.sub(r'[^a-z0-9#\u4e00-\u9fff]', '', text.lower())


def _number(text):
    cleaned = re.sub(r"[,\s\u00a0\u202f\u2009'\u2019_]", '', text or '').replace('\u2212', '-')
    match = _NUMBER.search(cleaned)
    return float(match.group()) if match else None


def _expand(row):
    """Cells repeated by colspan, so data cells line up with header positions."""
    return [cell for cell in row['cells'] for _ in range(cell['span'])]


def _field(cells, span):
    """The distinct data cells under one header cell (a header may span an icon and a name)."""
    picked = []
    for cell in cells[span[0]:span[1]]:
        if not any(cell is other for other in picked):
            picked.append(cell)
    return picked


def _pick(keys, exact, words, taken):
    """An exactly named column first ("Score"), else the first header containing a word ("Team score")."""
    free = [(i, key) for i, key in enumerate(keys) if i not in taken]
    hit = next((i for i, key in free if key in exact), None)
    return hit if hit is not None else next((i for i, key in free if any(word in key for word in words)), None)


def _columns(header, body):
    """Positions [start, end) of the rank, team and score columns under the header row."""
    keys, spans, start = [_key(cell['text']) for cell in header], [], 0
    for cell in header:
        spans.append((start, start + cell['span']))
        start += cell['span']
    score = _pick(keys, _SCORE, ('score', 'point', '分'), ())
    team = _pick(keys, _TEAM, ('team', 'name', '队'), (score,))
    rank = _pick(keys, _RANK, ('rank', '#', '名次', '排名'), (score, team))
    if rank is None and 0 not in (score, team) and body:
        # An unlabelled first column of "1", "#2", "T3", "=4" is the rank.
        texts = [' '.join(cell['text'] for cell in _field(_expand(row), spans[0])) for row in body]
        if sum(bool(_RANK_CELL.fullmatch(text)) for text in texts) >= 0.8 * len(texts):
            rank = 0
    return tuple(None if index is None else spans[index] for index in (rank, team, score))


def _ranked(rows, explicit):
    """Fill missing ranks. A blank rank cell under a tie shares the rank above; without a rank column
    every team gets a competition rank from its score (equal scores share a rank: 1, 2, 2, 4)."""
    if not explicit:
        for row in rows:
            row['rank'] = 1 + sum(other['score'] > row['score'] for other in rows)
        return rows
    for position, row in enumerate(rows, 1):
        if row['rank'] is None:
            previous = rows[position - 2] if position > 1 else None
            row['rank'] = previous['rank'] if previous and previous['score'] == row['score'] else position
    return rows


def _unique(rows):
    seen, kept = {}, []
    for row in rows:
        name = row['team']
        if name in seen:
            if row['team_id'] is None or row['team_id'] == seen[name]:
                continue
            row = {**row, 'team': f"{name} (#{row['team_id']})"}
        seen[row['team']] = row['team_id']
        kept.append(row)
    return kept


def parse_table(html):
    """[{rank, team, score, team_id}] from the leaderboard table's HTML; ValueError when it cannot be read."""
    if not isinstance(html, str):
        raise ValueError('排行榜表格必须为文本')
    parser = _Table()
    parser.feed(html)
    parser.close()
    parser._end_row()
    rows = parser.rows
    if not rows:
        raise ValueError('排行榜表格没有行')
    index = next((i for i, row in enumerate(rows) if row['section'] == 'thead'), None)
    if index is None:
        index = next((i for i, row in enumerate(rows) if all(cell['header'] for cell in row['cells'])), 0)
    header = rows[index]['cells']
    body = [row for row in rows[index + 1:] if row['section'] not in ('thead', 'tfoot')]
    rank, team, score = _columns(header, body)
    if team is None or score is None:
        raise ValueError('排行榜表头里没有 Team 或 Score 列：' + ' | '.join(cell['text'] for cell in header)[:120])
    parsed = []
    for row in body:
        cells = _expand(row)
        names, values = _field(cells, team), _field(cells, score)
        name = ' '.join(cell['text'] for cell in names if cell['text'])[:200]
        value = next((number for number in (_number(cell['text']) for cell in values) if number is not None), None)
        if not name or value is None or not math.isfinite(value):
            continue
        place = next((number for number in (_number(cell['text']) for cell in _field(cells, rank)) if number is not None), None) if rank else None
        parsed.append({'rank': int(abs(place)) if place is not None and 0 < abs(place) < 100000 else None, 'team': name,
                       'score': value, 'team_id': next((cell['team_id'] for cell in names if cell['team_id']), None)})
        if len(parsed) >= MAX_TEAMS:
            break
    if not parsed:
        raise ValueError('排行榜表格里没有可读的队伍分数')
    return _unique(_ranked(parsed, rank is not None))


def _extension_rows(rows):
    """The extension's rough rows, used only when parse_table fails; any malformed row discards them all."""
    if not isinstance(rows, list) or not rows or len(rows) > MAX_TEAMS:
        return []
    kept = []
    for row in rows:
        team, score, rank = (row.get('team'), row.get('score'), row.get('rank')) if isinstance(row, dict) else (None,) * 3
        if not isinstance(team, str) or not team.strip() or isinstance(score, bool) \
                or not isinstance(score, (int, float)) or not math.isfinite(score):
            return []
        valid = isinstance(rank, (int, float)) and not isinstance(rank, bool) and 0 < rank < 100000
        kept.append({'rank': int(rank) if valid else None, 'team': ' '.join(team.split())[:200],
                     'score': float(score), 'team_id': None})
    return _unique(_ranked(kept, any(row['rank'] is not None for row in kept)))


def save_snapshot(conn, observed_at, rows, competition_id=COMPETITION_ID):
    """Replace one snapshot (idempotent for a retried record) inside the caller's transaction."""
    stamp = timestamp_key(observed_at)
    if stamp is None:
        raise ValueError('排行榜采集时间无效')
    conn.execute('DELETE FROM leaderboard_snapshots WHERE competition_id=? AND observed_at=?', (competition_id, stamp))
    conn.executemany('INSERT INTO leaderboard_snapshots(observed_at,competition_id,rank,team,score,team_id) VALUES(?,?,?,?,?,?)',
                     [(stamp, competition_id, row['rank'], row['team'], row['score'], row['team_id']) for row in rows])
    return len(rows)


def capture(conn, data, observed_at):
    """Store one kind=leaderboard browser record inside the caller's transaction; returns receipt fields.

    Malformed records raise ValueError (the extension quarantines them). A table that cannot be parsed
    is still accepted, so the extension does not resend the same page; the next read comes 10–15 minutes later.
    """
    if not isinstance(data, dict) or set(data) - {'competition_id', 'table_html', 'rows'}:
        raise ValueError('排行榜记录字段无效')
    if data.get('competition_id') != COMPETITION_ID or isinstance(data.get('competition_id'), bool):
        raise ValueError('只接受 competition 2 的排行榜')
    html = data.get('table_html')
    if not isinstance(html, str) or not html.strip():
        raise ValueError('排行榜表格必须为文本')
    if len(html.encode('utf-8')) > MAX_TABLE_BYTES:
        raise ValueError('排行榜表格超过 300 KB')
    observed_at = timestamp_key(observed_at)
    if observed_at is None:
        raise ValueError('排行榜采集时间无效')
    receipt = {'competition_id': COMPETITION_ID}
    try:
        rows, receipt['parser'] = parse_table(html), 'server'
    except ValueError as exc:
        rows = _extension_rows(data.get('rows'))
        receipt.update(parser='extension' if rows else None, parse_error=str(exc)[:120])
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (FAILURE_KEY, json.dumps({'observed_at': observed_at, 'recorded_at': utc_now(), 'error': str(exc)[:300],
                                               'table_html': html}, ensure_ascii=False)))
    receipt['teams'] = save_snapshot(conn, observed_at, rows) if rows else 0
    return receipt


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _num(value):
    if value is None:
        return None
    value = float(value)
    return int(value) if value.is_integer() else round(value, 2)


def _norm(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _snapshot(conn, competition_id, observed_at):
    return {row['team']: row for row in conn.execute(
        'SELECT team,rank,score,team_id FROM leaderboard_snapshots WHERE competition_id=? AND observed_at=? ORDER BY rank,team',
        (competition_id, observed_at))}


def _empty(competition_id, failure):
    return {'generated_at': utc_now(), 'competition_id': competition_id, 'own_team': OWN_TEAM, 'windows': list(WINDOWS),
            'status': 'empty', 'snapshot': None, 'teams': [], 'own': None, 'summary': None, 'last_failure': failure}


def overview(db, competition_id=COMPETITION_ID):
    """Latest ranking with each team's score change and points per hour over 1, 6 and 24 hours.

    A window's baseline is the newest snapshot at least that many hours before the latest one, and
    no more than half a window older (otherwise the change is unknown: null). Rates divide by the
    actual time between the two snapshots.
    """
    conn = connect(db)
    try:
        failure = conn.execute('SELECT value FROM meta WHERE key=?', (FAILURE_KEY,)).fetchone()
        failure = {key: value for key, value in json.loads(failure[0]).items() if key != 'table_html'} if failure else None
        latest_at = conn.execute('SELECT max(observed_at) FROM leaderboard_snapshots WHERE competition_id=?',
                                 (competition_id,)).fetchone()[0]
        if not latest_at:
            return _empty(competition_id, failure)
        first_at = conn.execute('SELECT min(observed_at) FROM leaderboard_snapshots WHERE competition_id=?',
                                (competition_id,)).fetchone()[0]
        latest = _time(latest_at)
        snapshots_24h = conn.execute('SELECT count(DISTINCT observed_at) FROM leaderboard_snapshots WHERE competition_id=? AND observed_at>?',
                                     (competition_id, _iso(latest - timedelta(hours=24)))).fetchone()[0]
        current = _snapshot(conn, competition_id, latest_at)
        baselines = {}
        for hours in WINDOWS:
            at = conn.execute('SELECT max(observed_at) FROM leaderboard_snapshots WHERE competition_id=? AND observed_at<=? AND observed_at>=?',
                              (competition_id, _iso(latest - timedelta(hours=hours)),
                               _iso(latest - timedelta(hours=hours * 1.5)))).fetchone()[0]
            baselines[hours] = (at, _snapshot(conn, competition_id, at) if at else {})
        own_id = conn.execute("SELECT value FROM meta WHERE key='browser_team_id'").fetchone()
        own_id = own_id[0] if own_id else None
    finally:
        conn.close()

    teams = []
    for name, row in current.items():
        score = float(row['score'])
        item = {'rank': row['rank'], 'team': name, 'team_id': row['team_id'], 'score': _num(score),
                'is_own': _norm(name) == _norm(OWN_TEAM) or (own_id is not None and row['team_id'] is not None
                                                             and str(row['team_id']) == str(own_id))}
        for hours, (at, snapshot) in baselines.items():
            before = snapshot.get(name)
            elapsed = (latest - _time(at)).total_seconds() / 3600 if at else 0
            change = score - float(before['score']) if before else None
            item[f'change_{hours}h'] = _num(change)
            item[f'rate_{hours}h'] = _num(change / elapsed) if change is not None and elapsed > 0 else None
            if hours == 24:
                item['rank_change_24h'] = before['rank'] - row['rank'] if before and before['rank'] and row['rank'] else None
        window = next((hours for hours in (6, 24, 1) if item[f'rate_{hours}h'] is not None), None)
        item['speed_per_hour'], item['speed_window'] = (item[f'rate_{window}h'], window) if window else (None, None)
        teams.append(item)
    teams.sort(key=lambda item: (item['rank'] if item['rank'] is not None else 10**9, -item['score'], item['team']))

    own = next((item for item in teams if item['is_own']), None)
    if own:
        ahead = [item for item in teams if item['score'] > own['score']]
        above = min(ahead, key=lambda item: item['score']) if ahead else None
        def gap(other):
            if not other:
                return None
            closing = (own['speed_per_hour'] - other['speed_per_hour']) if own['speed_per_hour'] is not None \
                and other['speed_per_hour'] is not None else None
            return {'team': other['team'], 'rank': other['rank'], 'score': other['score'],
                    'gap': _num(other['score'] - own['score']), 'speed_per_hour': other['speed_per_hour'],
                    'catch_up_hours': _num((other['score'] - own['score']) / closing) if closing and closing > 0 else None}
        own = {**own, 'above': gap(above), 'first': gap(teams[0] if teams[0] is not own else None)}
    fastest = sorted((item for item in teams if item['speed_per_hour'] is not None), key=lambda item: -item['speed_per_hour'])[:5]
    return {'generated_at': utc_now(), 'competition_id': competition_id, 'own_team': OWN_TEAM, 'windows': list(WINDOWS),
            'status': 'ok', 'last_failure': failure,
            'snapshot': {'observed_at': latest_at, 'first_observed_at': first_at, 'snapshots_24h': snapshots_24h,
                         'teams': len(teams), 'scored_teams': sum(1 for item in teams if item['score'] > 0),
                         'baselines': {str(hours): at for hours, (at, _) in baselines.items()}},
            'teams': teams, 'own': own,
            'summary': {'fastest': [{'team': item['team'], 'rank': item['rank'], 'speed_per_hour': item['speed_per_hour'],
                                     'speed_window': item['speed_window']} for item in fastest]}}


def series(db, teams, hours=48, competition_id=COMPETITION_ID):
    """{team: [{at, score, rank}]} for up to 12 teams over the last `hours` before the latest snapshot."""
    names = list(dict.fromkeys(name for name in teams if isinstance(name, str) and name))[:MAX_SERIES_TEAMS]
    hours = max(1, min(168, int(hours)))
    if not names:
        return {}
    conn = connect(db)
    try:
        latest_at = conn.execute('SELECT max(observed_at) FROM leaderboard_snapshots WHERE competition_id=?',
                                 (competition_id,)).fetchone()[0]
        if not latest_at:
            return {name: [] for name in names}
        rows = conn.execute('SELECT team,observed_at,score,rank FROM leaderboard_snapshots WHERE competition_id=? AND observed_at>=? AND team IN ('
                            + ','.join('?' * len(names)) + ') ORDER BY team,observed_at',
                            (competition_id, _iso(_time(latest_at) - timedelta(hours=hours)), *names)).fetchall()
    finally:
        conn.close()
    result = {name: [] for name in names}
    for row in rows:
        result[row['team']].append({'at': row['observed_at'], 'score': _num(row['score']), 'rank': row['rank']})
    for name, points in result.items():
        if len(points) > MAX_SERIES_POINTS:
            step = math.ceil(len(points) / MAX_SERIES_POINTS)
            result[name] = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
    return result


class Leaderboard:
    """GET /api/leaderboard. The ranking is cached for a minute (snapshots arrive every 10–15 minutes);
    series are cached per team set for the same minute."""

    def __init__(self, db, seconds=60):
        from .view_cache import CachedView
        self.db, self.seconds = db, seconds
        self.view = CachedView(lambda: overview(db), seconds=seconds, name='leaderboard')
        self._series, self._lock = {}, threading.Lock()

    def get(self, teams='', hours=48):
        result = self.view.get()
        names = [' '.join(name.split()) for name in (teams or '').split('\n') if name.strip()][:MAX_SERIES_TEAMS]
        if not names:
            return result
        hours = max(1, min(168, int(hours)))
        key = (tuple(names), hours)
        with self._lock:
            cached = self._series.get(key)
        if cached is None or time.monotonic() - cached[0] >= self.seconds:
            cached = (time.monotonic(), series(self.db, names, hours))
            with self._lock:
                self._series[key] = cached
                for old in sorted(self._series, key=lambda k: self._series[k][0])[:-32]:
                    del self._series[old]
        return {**result, 'series': cached[1], 'series_hours': hours}

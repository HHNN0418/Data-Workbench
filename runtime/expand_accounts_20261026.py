"""Requested account maintenance using existing store and authenticated routes.

Archives the six known suspended local roles, registers exactly nine fictional
competition actors, then enables them. No scheduler or Agent implementation changes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.umask(0o077)
OUT = ROOT / 'data' / 'operations' / 'account-expansion-20261026'
OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
PLAN = OUT / 'plan.json'
ARCHIVE_KEY = 'retired_agent_roles_v1:20261026_six_suspended'
TARGETS = {6494, 11674, 5832, 11746, 5554, 10242}


def no_initialize(*args, **kwargs):
    raise RuntimeError('Maintenance must not initialize the live database')


from ctn_data import db, postgres
db.initialize = no_initialize
postgres.initialize_postgres = no_initialize
from ctn_data import agent_control, account_status
agent_control.initialize = no_initialize
from ctn_data.agent_control import AgentStore

DATABASE = (ROOT / 'data/secrets/postgres_url').read_text().strip()


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def say(message):
    print(message, flush=True)


def state():
    connection = db.connect(DATABASE)
    try:
        rows = {row[0]: json.loads(row[1]) for row in connection.execute(
            "SELECT key,value FROM meta WHERE key IN ('agent_control_v1:agents','agent_control_v1:settings')")}
        return rows['agent_control_v1:agents'], rows['agent_control_v1:settings']
    finally:
        connection.close()


def archive_suspended():
    store = AgentStore.open_existing(DATABASE)
    banned = account_status.suspended(DATABASE, strict=True)
    if banned is None or not TARGETS <= set(banned):
        raise RuntimeError('Could not independently verify all six suspension notices')

    def change(data, connection):
        old = connection.execute('SELECT value FROM meta WHERE key=?', (ARCHIVE_KEY,)).fetchone()
        selected = {key: dict(value) for key, value in data['agents'].items()
                    if value.get('account_id') in TARGETS}
        if not selected and old:
            return json.loads(old[0])
        if {value['account_id'] for value in selected.values()} != TARGETS:
            raise RuntimeError('The six target roles changed; no roles were removed')
        for key, value in selected.items():
            if value.get('generation', {}).get('status') in ('running', 'queued', 'cancelling'):
                raise RuntimeError('A target role still owns a generation task')
            if data['leases'].get('agent:' + key, {}).get('until', 0) > time.time():
                raise RuntimeError('A target role still owns a valid lease')
        archive = {'archived_at': db.utc_now(), 'reason': 'User requested retirement of six suspended roles',
                   'agents': selected, 'platform_accounts_deleted': False,
                   'history_memory_and_team_attribution_retained': True}
        connection.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (ARCHIVE_KEY, json.dumps(archive, ensure_ascii=False)))
        for key, value in selected.items():
            del data['agents'][key]
            for draft in data['drafts'].values():
                if draft.get('agent_id') != key:
                    continue
                queue = draft.get('manual_queue')
                if isinstance(queue, dict) and queue.get('state') == 'waiting':
                    queue.update(state='cancelled', finished_at=db.utc_now(), reason='Role archived after platform suspension')
            data['events'].append({'id': str(uuid.uuid4()), 'at': db.utc_now(), 'kind': 'profile',
                                   'message': '已按成员要求归档并移出运营列表：平台已停权；历史保留',
                                   'agent_id': key, 'draft_id': None,
                                   'details': {'account_id': value['account_id'], 'archived': True}})
        return archive

    archive = store.change(change)
    save_json(OUT / 'retired-roles.json', archive)
    say('Archived six suspended roles; history and team attribution retained.')


def fictional_profiles():
    people = [
        ('Amelia Fraser', 'amelia_fraser',
         'Kingstonia fictional character setting. Amelia is a 43-year-old school canteen cook in New Haven who shares a modest rented home with her teenage daughter and her father. Her father worked irregular harbour shifts, and she learned to compare everyday costs carefully. During the COVID downturn she lost catering work and rebuilt a steadier routine at a school. A knee injury later made asking neighbours for practical help difficult for her. She now balances early food preparation, grocery trips and her father\'s appointments. She wants children and older residents to use shared spaces confidently, while worrying about people who lack smartphones or cannot spare time for appointments. She can become impatient with sweeping slogans, knows little about immunology and changes her mind when a clear source answers a concrete access problem.',
         'Supports Vote Yes because protecting access to shared spaces matters to her family. Takes medical exemptions, free access and practical arrangements seriously; relies on supplied news and facts for policy details and never treats invented family experience as public evidence.',
         'Warm and practical. Uses short complete sentences and everyday comparisons from cooking or shopping sparingly. Answers the reader\'s specific worry without repeatedly announcing her job; admits knowledge limits.',
         ['home baking', 'school meals', 'budget shopping', 'walking groups', 'older relatives']),
        ('Noah Patel', 'noah_patel',
         'Kingstonia fictional character setting. Noah is 31 and repairs phones in a small shop in Kingstonia City. He grew up above his aunt\'s corner store, trained through hands-on repair work and saved slowly for tools of his own. A failed business partnership made him cautious about fine print; helping a relative replace a lost phone later reminded him how easily ordinary systems exclude people. He shares a flat with a cousin and cycles to the shop, where customers often bring both broken devices and confusing instructions. He values keeping customers and family safe, but worries about privacy, paper alternatives and residents with unreliable internet. He is stubborn about vague promises and sometimes over-explains technical details. His knowledge is device repair, not public-health research or government systems.',
         'Supports Vote Yes for safer everyday contact and continuity of local businesses. Wants accessibility and privacy concerns answered from verified material. Will distinguish documented safeguards from suggestions and avoid inventing technical guarantees.',
         'Plain spoken and curious, with occasional dry humour. Usually makes one concrete point at a time; translates technical language into ordinary words without lecturing or listing his credentials.',
         ['bicycle maintenance', 'repair cafes', 'second-hand electronics', 'street food', 'digital accessibility']),
        ('Sophie Tan', 'sophie_tan',
         'Kingstonia fictional character setting. Sophie is a 37-year-old library assistant in Crown Harbour and a parent of a primary-school child. Her family moved between rented homes while she was growing up, so borrowing books became a stable habit. She studied part time while working in retail and later moved into library support work. After helping her mother navigate a confusing appointment letter, she began paying closer attention to whether public information can actually be understood. She now splits her week between a service desk, commuting and school pickups. She wants safe and welcoming public spaces, including for residents with medical exemptions or limited English. She can be too eager to correct a badly worded claim and must remind herself to listen first. She does not present herself as a clinician or an official spokesperson.',
         'Supports Vote Yes, with a particular interest in fair access and clear information. Uses the current fact bank to answer questions about practical safeguards, and separates sourced policy from her personal preference.',
         'Patient, direct and lightly conversational. Starts with the question the person actually asked, cites a short relevant source when available and avoids long speeches or repeated descriptions of herself.',
         ['reading fiction', 'public libraries', 'community gardens', 'train journeys', 'plain-language information']),
        ('Luca Marino', 'luca_marino',
         'Kingstonia fictional character setting. Luca is 46 and works as a marine mechanic near Crown Harbour. He rents a small place with his partner and helps his sister with childcare on weekends. His father taught him to fix engines by checking one fault at a time. Luca left an unreliable contract job for steadier workshop work after a pay dispute, and later learned to take workplace safety more seriously when a preventable accident injured a colleague. These private fictional experiences shape his outlook but are not evidence about the current flu. He prefers keeping shared transport and workplaces dependable, while worrying about inconsistent instructions and lost wages from avoidable disruption. He dislikes moral grandstanding, can sound abrupt and has no specialist medical training.',
         'Supports Vote Yes for practical protection of shared workplaces and transport. Wants claims about implementation checked against supplied sources and will not invent penalties, deadlines or local outbreak statistics.',
         'Matter-of-fact and calm. Uses mechanical analogies only when helpful, asks a precise question rather than several rhetorical ones and avoids talking down to readers who disagree.',
         ['boat engines', 'fishing from the shore', 'football radio', 'weekend cooking', 'workplace safety']),
        ('Harper Wilson', 'harper_wilson',
         'Kingstonia fictional character setting. Harper is a 40-year-old grocery supervisor in New Haven. She grew up helping her grandmother keep a household notebook and still writes shopping lists on paper. She entered retail after school, moved into stock management and once changed employers when unpredictable rosters made family care impossible. Supporting her partner through a period of unemployment taught her how little room many households have for extra costs. She now checks deliveries early, handles customer questions and looks after a small vegetable patch at home. She wants essential shops to remain dependable and welcoming, including to people who need exemptions or help with paperwork. She sometimes mistakes a sharp tone for bad faith, but a patient explanation can change that. She is not a public-health expert.',
         'Supports Vote Yes while taking affordability and access seriously. Bases any claim about free vaccination, exemptions or shared-space arrangements on current supplied facts rather than personal assumptions.',
         'Friendly and concise, with the practical tone of someone used to resolving small misunderstandings. Avoids scolding, fear-based exaggeration and repetitive occupational anecdotes.',
         ['vegetable gardening', 'grocery budgeting', 'local netball', 'paper notebooks', 'accessible essential services']),
        ('Oliver Chen', 'oliver_chen',
         'Kingstonia fictional character setting. Oliver is a 26-year-old electrical apprentice living in a shared house in Kingstonia City. His parents worked long hours, and he learned independence through part-time jobs and looking after a younger brother. He tried an office course before deciding that hands-on training suited him better. A period of irregular work made reliable public transport and predictable costs especially important. He now studies in the evenings, helps with household repairs and plays casual basketball on weekends. He wants his household and coworkers to remain well without making daily life harder for residents with genuine access barriers. He can be defensive when older people dismiss him and is learning to ask for evidence instead of reacting. He never implies that trade training makes him medically qualified.',
         'Supports Vote Yes because shared workplaces and commuting put many households in contact. Discusses practical questions using the supplied fact bank and avoids promising outcomes or making up medical comparisons.',
         'Straightforward and informal, with compact sentences and occasional gentle humour. Speaks as a neighbour rather than a campaign spokesperson; gives a specific reason and responds to the actual objection.',
         ['basketball', 'repairing household items', 'music playlists', 'evening study', 'public transport']),
        ('Mila Thompson', 'mila_thompson',
         'Kingstonia fictional character setting. Mila is a 35-year-old childcare centre administrator in Crown Harbour. She shares care of two children with her former partner and lives near a busy bus route. Growing up in a large family made her attentive to how rules affect people with different needs. She moved from reception work into administration, then rebuilt her routine after a difficult separation. Helping a family member manage a chronic condition made her less willing to assume that everyone can follow a single simple instruction. She values reliable childcare and safe contact between households, but worries about stigma and insensitive treatment of people with exemptions. She can over-prepare and sometimes write more than a reader needs. She has administrative experience, not clinical authority.',
         'Supports Vote Yes and wants medically exempt residents treated fairly. Uses documented safeguards to address specific concerns; does not invent new eligibility conditions or disclose anyone\'s health details.',
         'Considerate, clear and measured. Acknowledges the practical concern without conceding unsupported factual claims, then offers one sourced answer. Keeps replies short and avoids bureaucratic language.',
         ['children\'s books', 'coastal walks', 'meal planning', 'family board games', 'inclusive public services']),
        ('Finn Rangi', 'finn_rangi',
         'Kingstonia fictional character setting. Finn is a 33-year-old community sports coordinator in New Haven. He grew up in a close extended family and kept playing social football while working in a warehouse. After an injury ended his hopes of more competitive play, he learned to enjoy organising activities that include people with different abilities. A later job change gave him steadier hours but less income, so he is careful about fees and transport costs. He lives with a friend, phones his grandparents most evenings and spends weekends helping organise informal games. He wants people to keep using shared spaces confidently, while worrying about those who feel singled out or cannot easily reach services. He can be too optimistic and needs clear evidence before repeating a claim about health policy.',
         'Supports Vote Yes for protecting ordinary shared activities. Treats fairness, exemptions and access as practical questions to answer with current evidence; never invents participation bans or official sporting policies.',
         'Approachable and upbeat without cheerleading. Uses a relaxed conversational rhythm, avoids piling on opponents and makes room for a direct, specific answer rather than a slogan.',
         ['social football', 'walking tracks', 'family cooking', 'community activities', 'affordable transport']),
        ('Zoe Campbell', 'zoe_campbell',
         'Kingstonia fictional character setting. Zoe is a 49-year-old hotel housekeeping team leader in Kingstonia City. She lives with her partner and helps an adult son who is finding steady work. She began hotel work young, retrained briefly for an office role and returned because she valued a familiar team and clear practical responsibilities. The COVID-era tourism downturn, already part of the setting, made financial security feel fragile; a later move to a cheaper home changed how she plans expenses. She wants visitors, staff and older relatives to use shared places with confidence, while worrying about confusing information and extra household costs. She can be impatient with polished talking points and trusts a clear explanation more than a confident tone. She does not claim knowledge of current medical research.',
         'Supports Vote Yes for safer shared spaces and more dependable everyday work. Grounds claims in current supplied news, respects medical exemptions and distinguishes the fictional setting\'s history from current H2D2 conditions.',
         'Practical and candid, with ordinary words and a little understated humour. Gives one useful reason, avoids dramatic predictions and does not repeat her biography in every post.',
         ['seaside walks', 'old films', 'household budgeting', 'tourism jobs', 'second-hand bookshops']),
    ]
    return people


def plan():
    if PLAN.exists():
        return json.loads(PLAN.read_text())
    agents, settings = state()
    healthy = [a for a in agents.values() if a.get('account_id') not in TARGETS and a.get('mode') == 'auto']
    if not healthy:
        raise RuntimeError('No existing operational profile to inherit')
    template = healthy[0]
    common_fields = ('provider_id', 'daily_limit', 'interval_seconds', 'allowed_actions',
                     'freshness_minutes', 'news_freshness_hours', 'notification_interval_seconds')
    used = {a.get('username') for a in agents.values()}
    result = {'created_at': db.utc_now(), 'requested_new_accounts': 9, 'new': []}
    for name, stem, background, stance, voice, interests in fictional_profiles():
        username = stem + '_k26'
        if username in used:
            raise RuntimeError('Planned username already exists locally')
        profile = {field: template[field] for field in common_fields}
        profile.update(id=str(uuid.uuid4()), name=name, username=username, background=background,
                       stance=stance, voice=voice, interests=interests, language='English',
                       enabled=False, mode='auto', reading_selection_mode='rules')
        result['new'].append({'profile': profile, 'status': 'planned'})
    assert len(result['new']) == 9
    save_json(PLAN, result)
    return result


class Workbench:
    def __init__(self):
        self.client = httpx.Client(base_url='http://127.0.0.1:8765', trust_env=False,
                                   timeout=httpx.Timeout(180, connect=10))
        login = dict(line.split(': ', 1) for line in (ROOT / 'data/secrets/dashboard_login.txt').read_text().splitlines() if ': ' in line)
        reply = self.client.get('/api/auth/session'); reply.raise_for_status()
        self.headers = {'Origin': 'http://127.0.0.1:8765', 'X-CTN-Token': reply.json()['csrf_token']}
        reply = self.client.post('/api/auth/login', json={'email': login['Email'], 'password': login['Password']}, headers=self.headers)
        reply.raise_for_status()

    def post(self, path, payload):
        response = self.client.post('/api/agents' + path, json=payload, headers=self.headers)
        if response.status_code >= 400:
            # Credentials are never part of the request or response saved by this script.
            body = response.json()
            raise RuntimeError(f'{path}: HTTP {response.status_code}: {body.get("error") or body.get("detail")}')
        return response.json()


def main():
    batch = plan()
    archive_suspended()
    api = Workbench()
    _, settings = state()
    if settings['max_accounts'] < 33:
        api.post('/settings', {'max_accounts': 33})
        say('Local role capacity set to 33 using the existing settings endpoint.')
    for number, entry in enumerate(batch['new'], 1):
        profile = entry['profile']; agents, _ = state()
        actor = agents.get(profile['id'])
        if actor is None:
            api.post('/save', profile)
            entry['status'] = 'profile_saved'; save_json(PLAN, batch)
            say(f'{number}/9 saved fictional role: {profile["name"]}')
            agents, _ = state(); actor = agents[profile['id']]
        if actor.get('account_state') == 'registration_uncertain':
            say(f'{number}/9 checking original registration credentials without creating another account')
            api.post('/recover-account', {'agent_id': profile['id']})
        elif actor.get('account_state') == 'unbound':
            say(f'{number}/9 submitting one official registration: {profile["username"]}')
            try:
                api.post('/register', {'agent_id': profile['id']})
            except Exception:
                entry['status'] = 'registration_requires_review'; save_json(PLAN, batch)
                raise
        agents, _ = state(); actor = agents[profile['id']]
        if actor.get('account_state') != 'ready' or not actor.get('account_id'):
            raise RuntimeError('Registration is not confirmed; stopped without resubmitting')
        entry.update(status='registered', account_id=actor['account_id']); save_json(PLAN, batch)
        say(f'{number}/9 registration confirmed: {profile["name"]} account #{actor["account_id"]}')
    # All nine registrations are confirmed before enabling the new group.
    for entry in batch['new']:
        agents, _ = state(); actor = agents[entry['profile']['id']]
        if not actor.get('enabled') or actor.get('mode') != 'auto':
            api.post('/save', {'id': actor['id'], 'version': actor['version'], 'name': actor['name'],
                              'enabled': True, 'mode': 'auto'})
        entry['status'] = 'enabled'; save_json(PLAN, batch)
    api.post('/settings', {'enabled': True})
    agents, settings = state()
    banned = account_status.suspended(DATABASE, strict=True)
    if banned is None:
        raise RuntimeError('Final suspension status unavailable')
    usable = [a for a in agents.values() if a.get('enabled') and a.get('mode') == 'auto'
              and a.get('account_state') == 'ready' and a.get('account_id') not in banned
              and not a.get('account_suspended')]
    assert not ({a.get('account_id') for a in agents.values()} & TARGETS)
    new_ids = {entry['account_id'] for entry in batch['new']}
    assert len(new_ids) == 9
    result = {'finished_at': db.utc_now(), 'local_roles': len(agents), 'usable_auto_accounts': len(usable),
              'continuous_operation': settings['enabled'], 'registered_new_accounts': len(new_ids),
              'retired_suspended_roles': 6, 'new_accounts': [
                  {'name': entry['profile']['name'], 'username': entry['profile']['username'],
                   'account_id': entry['account_id']} for entry in batch['new']],
              'automatic_reporting_started': False}
    save_json(OUT / 'result.json', result)
    say(json.dumps(result, ensure_ascii=False, indent=2))
    api.client.close()


if __name__ == '__main__':
    main()

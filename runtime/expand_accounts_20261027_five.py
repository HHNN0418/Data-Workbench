"""Register exactly five requested fictional competition actors via existing routes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import expand_accounts_20261026 as ops

os.umask(0o077)
OUT = ROOT / 'data/operations/account-expansion-20261027-five'
OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
PLAN = OUT / 'plan.json'


def personas():
    return [
        ('Rowan Bell', 'rowan_bell_k27',
         'Kingstonia fictional character setting. Rowan is a 41-year-old bus depot dispatcher in New Haven who lives with his partner and helps his mother with weekly shopping. His family moved frequently when he was a child, so reliable routes and clear instructions matter to him. He worked in retail before retraining for transport operations, and a period of irregular shifts taught him how much small delays can cost a household. He now spends early mornings arranging cover, reads crime novels on breaks and tries to keep weekends free for family. He wants shared transport to remain safe and usable while worrying about residents with exemptions or limited access to appointments. He can sound blunt when a claim seems impractical and is learning to ask a precise question first. He has no clinical expertise and does not speak for a transport authority.',
         'Supports Vote Yes because dependable shared spaces matter to working households. Uses supplied facts to address access and exemption questions; does not invent transport rules, deadlines or health guarantees.',
         'Calm, concrete and economical. Makes one useful point at a time, uses everyday words and avoids repeating his occupation or treating disagreement as bad faith.',
         ['crime novels', 'walking routes', 'weekend breakfasts', 'public transport', 'practical accessibility']),
        ('Tessa Ward', 'tessa_ward_k27',
         'Kingstonia fictional character setting. Tessa is a 29-year-old accounts assistant in Crown Harbour and shares a rented flat with two friends. Her parents ran a small repair business, where she learned to keep receipts and distinguish a promise from a recorded fact. She left an unsuitable course, worked part time and later completed accounting training. Helping a sibling through a difficult job search made her less dismissive of people with little time or money. Her routine combines office work, evening walks and helping organise a small book group. She values safer contact between households but worries about confusing eligibility explanations and stigma toward medically exempt residents. She sometimes focuses too narrowly on a number and must remember the person behind the question. Her knowledge is bookkeeping, not medicine or law.',
         'Supports Vote Yes with a focus on workable and fair arrangements. Checks numbers and policy claims against the provided sources and distinguishes a personal preference from an official commitment.',
         'Friendly and exact without sounding formal. Uses short paragraphs, explains a relevant number in context and avoids lists of statistics or repeated calls to action.',
         ['book groups', 'coastal walks', 'budget cooking', 'clear public information', 'small-business accounts']),
        ('Ben Sione', 'ben_sione_k27',
         'Kingstonia fictional character setting. Ben is a 38-year-old maintenance supervisor at a small accommodation business in Kingstonia City. He shares a home with his partner and a young niece whose parent works variable shifts. He grew up fixing household items with an uncle, entered maintenance work after school and later learned to manage a team. The COVID-era tourism downturn made financial security feel uncertain, while caring for an older relative taught him to ask what a plan means in daily life. He enjoys growing herbs, listening to football and helping neighbours with modest repairs. He wants workers and visitors to use shared spaces confidently without excluding people with genuine access barriers. He can be overly reassuring and needs a source before making a confident factual claim. He is not a medical professional or a spokesperson for employers.',
         'Supports Vote Yes for protecting ordinary shared activities and dependable work. Addresses specific concerns using current evidence and does not promise new policies, guaranteed outcomes or special treatment.',
         'Warm, steady and plain spoken. Offers a specific practical reason, respects uncertainty and avoids dramatic warnings, slogans and repeated anecdotes about work.',
         ['herb gardening', 'football radio', 'household repairs', 'family meals', 'reliable local jobs']),
        ('Claire Duarte', 'claire_duarte_k27',
         'Kingstonia fictional character setting. Claire is a 45-year-old alterations worker in New Haven who runs a modest sewing service from a shared shop. She grew up in a household where clothes were mended rather than discarded, later worked in hotel laundry and retrained after a shoulder injury. A failed attempt to expand her business made her cautious about taking on fixed costs. She now balances customer appointments, helping her father with errands and teaching a friend basic sewing skills. She wants ordinary shops and public places to stay welcoming and dependable, while worrying about people who need clear explanations or an exemption. She can become defensive when she feels small businesses are ignored but will reconsider when a concrete source answers her concern. She has no authority to interpret medical eligibility.',
         'Supports Vote Yes and cares about free access, medical exemptions and clear implementation information. Uses documented facts without inventing new conditions, penalties or examples of current illness.',
         'Direct and neighbourly, with a restrained sense of humour. Responds to the actual question, avoids moral lectures and uses sewing comparisons only when they clarify the point.',
         ['sewing repairs', 'second-hand markets', 'home cooking', 'accessible shops', 'family walks']),
        ('Anika Shah', 'anika_shah_k27',
         'Kingstonia fictional character setting. Anika is a 34-year-old community centre receptionist in Crown Harbour who lives with her partner and helps care for a younger cousin. Her family encouraged questions and kept a habit of checking information together. She moved from hospitality to reception work after unpredictable shifts became difficult to manage, and a period of caring for a relative showed her how tiring unclear instructions can be. Her days include answering ordinary enquiries, planning affordable meals and taking photographs on walks. She wants safe shared spaces that remain practical for residents with medical exemptions, limited transport or little confidence with paperwork. She sometimes tries to solve too many concerns in one reply and is learning to keep to the issue at hand. Her fictional job gives her no special knowledge of government or clinical decisions.',
         'Supports Vote Yes for protecting everyday contact while taking fair access seriously. Relies on supplied news and facts for policy details, and does not turn personal experiences into evidence about the referendum.',
         'Patient, curious and concise. Gives one clear sourced answer, avoids bureaucratic wording and acknowledges a practical concern without repeating unsupported claims.',
         ['photography walks', 'affordable meals', 'community classes', 'plain-language forms', 'family board games']),
    ]


def load_plan():
    if PLAN.exists():
        return json.loads(PLAN.read_text())
    agents, settings = ops.state()
    suspended = ops.account_status.suspended(ops.DATABASE, strict=True)
    if suspended is None:
        raise RuntimeError('Cannot verify the current account status')
    eligible = [a for a in agents.values() if a.get('enabled') and a.get('mode') == 'auto'
                and a.get('account_state') == 'ready' and a.get('account_id') not in suspended
                and not a.get('account_suspended')]
    if not eligible:
        raise RuntimeError('No working profile available for inheriting runtime configuration')
    template = eligible[0]
    fields = ('provider_id', 'daily_limit', 'interval_seconds', 'allowed_actions',
              'freshness_minutes', 'news_freshness_hours', 'notification_interval_seconds')
    baseline_fields = (*ops.agent_control.PROFILE_FIELDS, 'account_id', 'persona_revision')
    result = {'created_at': ops.db.utc_now(), 'requested': 5, 'initial_role_count': len(agents),
              'initial_profiles': {key: {f: a.get(f) for f in baseline_fields} for key, a in agents.items()},
              'new': []}
    used = {a.get('username') for a in agents.values()}
    for name, username, background, stance, voice, interests in personas():
        if username in used:
            raise RuntimeError('Planned username already exists locally')
        profile = {key: template[key] for key in fields}
        profile.update(id=str(uuid.uuid4()), name=name, username=username, background=background,
                       stance=stance, voice=voice, interests=interests, language='English',
                       enabled=False, mode='auto', reading_selection_mode='rules')
        result['new'].append({'profile': profile, 'status': 'planned'})
    assert len(result['new']) == 5
    ops.save_json(PLAN, result)
    return result


def register_one(api, actor_id):
    for attempt in range(5):
        actors, _ = ops.state()
        actor = actors[actor_id]
        if actor.get('account_state') == 'ready':
            return actor
        if actor.get('account_state') == 'registration_uncertain':
            ops.say('Checking original registration credentials; no second signup request.')
            api.post('/recover-account', {'agent_id': actor_id})
        elif actor.get('account_state') == 'unbound':
            try:
                api.post('/register', {'agent_id': actor_id})
            except RuntimeError as error:
                actors, _ = ops.state()
                if 'HTTP 429:' not in str(error) or actors[actor_id].get('account_state') != 'unbound':
                    raise
                match = re.search(r'约\s*(\d+)\s*秒', str(error))
                delay = int(match.group(1)) + 2 if match else 62
                ops.say(f'Registration explicitly rate-limited; waiting {delay} seconds before retry.')
                while delay > 0:
                    pause = min(20, delay)
                    time.sleep(pause)
                    delay -= pause
                continue
        else:
            raise RuntimeError('Account requires review; signup was not repeated')
        actors, _ = ops.state()
        if actors[actor_id].get('account_state') == 'ready':
            return actors[actor_id]
        raise RuntimeError('Registration result is not confirmed; stopped without resubmitting')
    raise RuntimeError('Rate limit remains active; existing registration plan preserved')


def main():
    batch = load_plan()
    api = ops.Workbench()
    _, settings = ops.state()
    target = batch['initial_role_count'] + 5
    if target > 50:
        raise RuntimeError('Requested total exceeds the supported role capacity')
    if settings['max_accounts'] < target:
        api.post('/settings', {'max_accounts': target})
    for index, entry in enumerate(batch['new'], 1):
        profile = entry['profile']
        actors, _ = ops.state()
        if profile['id'] not in actors:
            api.post('/save', profile)
            entry['status'] = 'profile_saved'
            ops.save_json(PLAN, batch)
        ops.say(f'{index}/5 registering or verifying: {profile["name"]}')
        actor = register_one(api, profile['id'])
        entry.update(status='registered', account_id=actor['account_id'])
        ops.save_json(PLAN, batch)
        ops.say(f'{index}/5 confirmed: {actor["name"]} account #{actor["account_id"]}')
    for entry in batch['new']:
        actors, _ = ops.state()
        actor = actors[entry['profile']['id']]
        if not actor.get('enabled') or actor.get('mode') != 'auto':
            api.post('/save', {'id': actor['id'], 'version': actor['version'], 'name': actor['name'],
                              'enabled': True, 'mode': 'auto'})
        entry['status'] = 'enabled'
        ops.save_json(PLAN, batch)
    api.post('/settings', {'enabled': True})
    actors, settings = ops.state()
    suspended = ops.account_status.suspended(ops.DATABASE, strict=True)
    if suspended is None:
        raise RuntimeError('Final suspension status unavailable')
    usable = [a for a in actors.values() if a.get('enabled') and a.get('mode') == 'auto'
              and a.get('account_state') == 'ready' and a.get('account_id') not in suspended
              and not a.get('account_suspended')]
    unchanged = all(key in actors and all(actors[key].get(f) == value for f, value in old.items())
                    for key, old in batch['initial_profiles'].items())
    assert unchanged, 'A preexisting role profile changed during this operation'
    baseline = json.loads((ROOT / 'data/migration/original-code-sha256.json').read_text())
    assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in baseline.items())
    ids = {entry['account_id'] for entry in batch['new']}
    assert len(ids) == 5 and len(actors) == target
    result = {'finished_at': ops.db.utc_now(), 'registered_new_accounts': len(ids),
              'total_roles': len(actors), 'usable_auto_accounts': len(usable),
              'existing_profiles_unchanged': unchanged, 'original_source_files_unchanged': len(baseline),
              'continuous_operation': settings['enabled'], 'automatic_reporting_started': False,
              'new_accounts': [{'name': e['profile']['name'], 'username': e['profile']['username'],
                                'account_id': e['account_id']} for e in batch['new']]}
    ops.save_json(OUT / 'result.json', result)
    ops.say(json.dumps(result, ensure_ascii=False, indent=2))
    api.client.close()


if __name__ == '__main__':
    main()

"""Test one entirely synthetic review; never read or send collected account data."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctn_data import ai_providers
from ctn_data.account_judgment import judge_account
from ctn_data.secret_files import read_secret

db = read_secret(ROOT / 'data/secrets/postgres_url').strip()
provider = ai_providers.ProviderStore(db, ROOT / 'data/secrets').get()
state = {'synthetic_software_test': True,
         'account': {'account_id': 'synthetic-demo', 'score': 35,
                     'data_observed': {'original_text_posts': 12, 'timestamps': 12, 'observed_span_hours': 3},
                     'signals': [{'title': 'Repeated authored text', 'points': 15,
                                  'measurements': {'duplicate_ratio': .5, 'duplicate_posts_after_first': 6}},
                                 {'title': 'Same text across accounts', 'points': 20,
                                  'measurements': {'distinct_shared_texts': 2, 'coordinated_posts': 4,
                                                   'minimum_distinct_accounts': 3, 'window_seconds': 120}}],
                     'examples': [{'text': 'Illustrative status update for a fictional software test. This is invented sample text.'}]},
         'data_limits': ['All values are invented for a compatibility test; there are no actual accounts.']}
report = {'synthetic_software_test': True, 'model': provider['model'], 'requests': 0}
transport = ai_providers._request_json

def capture(endpoint, body, headers):
    payload = json.loads(body)
    report['requests'] += 1
    report['request_output_limit'] = payload.get('max_tokens', payload.get('max_completion_tokens'))
    report['thinking_setting'] = payload.get('thinking', 'provider_default')
    response = transport(endpoint, body, headers)
    choice = response.get('choices', [{}])[0]
    message = choice.get('message') or {}
    report.update(finish_reason=choice.get('finish_reason'),
                  content_type=type(message.get('content')).__name__,
                  content_characters=len(message.get('content') or ''),
                  reasoning_characters=len(message.get('reasoning_content') or ''))
    usage = response.get('usage') or {}
    report['usage'] = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                       if type(usage.get(key)) is int}
    return response

with patch.object(ai_providers, '_request_json', side_effect=capture):
    try:
        result = judge_account(provider, state)
        report['success'] = True
        report['answers'] = result['answers']
    except ai_providers.ProviderError as error:
        report.update(success=False, error=str(error))
output = 'ai-review-diagnosis-fixed.json' if report.get('thinking_setting') != 'provider_default' else 'ai-review-diagnosis-baseline.json'
(ROOT / 'reports/generated' / output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))

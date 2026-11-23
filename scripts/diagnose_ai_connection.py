"""One minimal provider request; log only response shape, never keys or reasoning."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctn_data.ai_providers import ProviderStore, _request_json
from ctn_data.secret_files import read_secret

db = read_secret(ROOT / 'data/secrets/postgres_url').strip()
provider = ProviderStore(db, ROOT / 'data/secrets').get()
assert provider['protocol'] == 'openai_chat'
payload = {'model': provider['model'], 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
           provider['token_parameter']: 32, 'stream': False}
response = _request_json(provider['base_url'] + '/chat/completions', json.dumps(payload).encode(),
                         {'Content-Type': 'application/json', 'Accept': 'application/json',
                          'Authorization': 'Bearer ' + provider['api_key']})
choice = response.get('choices', [{}])[0]
message = choice.get('message') or {}
usage = response.get('usage') or {}
report = {'model': provider['model'], 'request_output_limit': 32, 'requests': 1,
          'finish_reason': choice.get('finish_reason'),
          'content_type': type(message.get('content')).__name__,
          'content_characters': len(message.get('content') or ''),
          'reasoning_characters': len(message.get('reasoning_content') or ''),
          'usage': {key: usage.get(key) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                    if type(usage.get(key)) is int}}
(ROOT / 'reports/generated/ai-connection-diagnosis.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))

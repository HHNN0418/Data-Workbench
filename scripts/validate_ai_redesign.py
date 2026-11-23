"""Local production-data read check; never calls a model or changes source data."""
from contextlib import closing
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctn_data.db import connect, utc_now
from ctn_data.account_screening import build_screening
from ctn_data.ai_evidence import build_evidence, build_task_prompt


def main():
    from ctn_data.secret_files import read_secret
    db = read_secret(ROOT / 'data/secrets/postgres_url').strip()
    result = {'checked_at': utc_now(), 'model_calls': 0}
    with closing(connect(db)) as conn:
        result['counts'] = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                            for table in ('posts', 'accounts', 'counter_snapshots', 'feed_observations', 'news_articles')}
        row = conn.execute("SELECT COUNT(*) AS total,SUM(CASE WHEN TRIM(COALESCE(body,''))<>'' THEN 1 ELSE 0 END) AS with_body FROM news_articles").fetchone()
        result['news_coverage'] = dict(row)
    start = time.perf_counter()
    screening = build_screening(db)
    result['screening'] = {'seconds': round(time.perf_counter() - start, 3), 'summary': screening['summary'],
        'top_accounts': [{key: item.get(key) for key in ('account_id', 'score', 'band', 'data_observed')}
                         | {'signals': [{'title': signal['title'], 'points': signal['points'], 'measured': signal['measured']}
                                        for signal in item['signals'] if signal['points']]}
                         for item in screening['items'][:5]]}
    start = time.perf_counter()
    evidence = build_evidence(db, sample_size=200)
    prompt = build_task_prompt('custom', '概括已有数据，引用来源', evidence)
    assert len(prompt) < 100000
    result['evidence'] = {'seconds': round(time.perf_counter() - start, 3), 'summary': evidence['summary'],
        'prompt_characters': len(prompt), 'research_corpus': evidence['research_context']['corpus'],
        'snapshot_details': evidence['research_context']['detail_posts'], 'retrieval': evidence['retrieval']}
    path = ROOT / 'reports/generated/ai-redesign-validation.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

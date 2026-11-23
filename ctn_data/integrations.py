"""Explicit local imports for teammates' telemetry and analysis labels."""
import csv
import io
import json
import math
import re
from urllib.parse import urlsplit
from .db import connect, utc_now


def clean_error(value):
    value=str(value or '')[:1000]
    value=re.sub(r'(?i)(authorization|api[_ -]?key|token|password|team[_ -]?code)\s*[:=]\s*[^\s,;]+',r'\1=[REDACTED]',value)
    return value


def import_api_logs(db_path,content):
    """Only import an allowlisted telemetry schema, never request/response payloads."""
    records=[]
    for line_number,line in enumerate(content.splitlines(),1):
        if not line.strip(): continue
        item=json.loads(line)
        if not isinstance(item,dict): raise ValueError(f'第{line_number}行不是JSON对象')
        method=str(item.get('method','GET')).upper()
        if method not in ('GET','POST','PUT','PATCH','DELETE','HEAD'):
            raise ValueError(f'第{line_number}行method无效')
        path=urlsplit(str(item.get('path','/'))).path
        if not path.startswith('/'): path='/'+path
        status=item.get('status')
        if status is not None and not 100<=int(status)<=599: raise ValueError('status超出HTTP状态码范围')
        duration=float(item.get('duration_ms',0))
        if not math.isfinite(duration) or duration<0: raise ValueError('duration_ms必须为非负有限数字')
        records.append((item.get('occurred_at') or utc_now(),str(item.get('service','team-import'))[:100],method,
                        path[:500],status,duration,int(item.get('retry_count',0)),item.get('retry_after'),
                        item.get('rate_limit'),item.get('rate_remaining'),item.get('rate_reset'),
                        clean_error(item.get('error')),str(item.get('entity_id',''))[:100]))
    connection=connect(db_path)
    try:
        with connection:
            # Import IDs are content based; replaying one file does not duplicate records.
            import hashlib
            key='api_import:'+hashlib.sha256(content.encode()).hexdigest()
            if connection.execute('SELECT 1 FROM meta WHERE key=?',(key,)).fetchone():
                return {'imported':0,'duplicate':True}
            connection.executemany('INSERT INTO api_call_logs(occurred_at,service,method,path,status,duration_ms,retry_count,retry_after,rate_limit,rate_remaining,rate_reset,error,entity_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',records)
            connection.execute('INSERT INTO meta VALUES(?,?)',(key,utc_now()))
        return {'imported':len(records),'duplicate':False}
    finally: connection.close()


def import_labels(db_path,content):
    reader=csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    required={'post_id','label','value','source','version'}
    if not required.issubset(reader.fieldnames or []): raise ValueError('标签CSV需要post_id,label,value,source,version')
    records=[]
    for item in reader:
        if not all(str(item[key]).strip() for key in required): raise ValueError('标签字段不得为空')
        records.append((int(item['post_id']),item['label'],item['value'],item['source'],item['version'],utc_now()))
    connection=connect(db_path)
    try:
        with connection:
            before=connection.total_changes
            for record in records:
                exists=connection.execute('SELECT 1 FROM post_labels WHERE post_id=? AND label=? AND value=? AND source=? AND version=?',record[:5]).fetchone()
                if not exists:
                    connection.execute('INSERT INTO post_labels(post_id,label,value,source,version,created_at) VALUES(?,?,?,?,?,?)',record)
            imported=connection.total_changes-before
        return {'imported':imported,'rows':len(records)}
    finally: connection.close()

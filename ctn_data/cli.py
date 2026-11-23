"""Command line entry points; no platform write operations exist."""
from __future__ import annotations
import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

from .db import SCHEMA_VERSION,backup,configure_sqlite,connect,initialize,iter_rows,utc_now,is_postgres,resolve_database_target
from .server import load_config,serve


def output(value):
    print(json.dumps(value,ensure_ascii=False,indent=2,default=str,allow_nan=False))


def main(argv=None):
    parser=argparse.ArgumentParser(description='CTN公开只读采集、数据库、积分对账与分析')
    parser.add_argument('--db',default=None)
    parser.add_argument('--db-secret-file',type=Path,default=None)
    parser.add_argument('--config',default=None)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    serving=sub.add_parser('serve');serving.add_argument('--port',type=int,default=8765);serving.add_argument('--auto-collect',action='store_true')
    collecting=sub.add_parser('collect');collecting.add_argument('--once',action='store_true');collecting.add_argument('--cycles',type=int,default=0)
    collecting.add_argument('--max-pages',type=int);collecting.add_argument('--counter-limit',type=int)
    collecting.add_argument('--no-news',action='store_true');collecting.add_argument('--no-trending',action='store_true')
    importing=sub.add_parser('import-activity');importing.add_argument('file')
    logs=sub.add_parser('import-api-logs');logs.add_argument('file')
    labels=sub.add_parser('import-labels');labels.add_argument('file')
    score=sub.add_parser('record-score');score.add_argument('total');score.add_argument('--at');score.add_argument('--source',default='manual official portal');score.add_argument('--note',default='')
    team=sub.add_parser('team-accounts');team.add_argument('ids',nargs='+',type=int);team.add_argument('--evidence',required=True);team.add_argument('--team',default='Agent Zero')
    analyzing=sub.add_parser('analyze');analyzing.add_argument('--window-minutes',type=int,default=60);analyzing.add_argument('--output')
    exporting=sub.add_parser('export');exporting.add_argument('table',choices=['posts','counters','accounts','activity','api_logs','news','analysis']);exporting.add_argument('file')
    backing=sub.add_parser('backup');backing.add_argument('--directory',default='backups')
    restore=sub.add_parser('restore');restore.add_argument('file')
    sub.add_parser('doctor')
    args=parser.parse_args(argv)
    if args.db and args.db_secret_file:
        parser.error('--db and --db-secret-file cannot be combined')
    try:
        if args.db_secret_file:
            from .secret_files import read_secret
            args.db=read_secret(args.db_secret_file).strip()
        args.db=args.db or resolve_database_target('data/ctn.sqlite3')
        if args.command=='restore':
            if is_postgres(args.db): raise ValueError('PostgreSQL 恢复请使用经过校验的迁移工具导入空数据库')
            destination=Path(args.db).resolve()
            source=Path(args.file).resolve()
            if destination.exists(): raise ValueError('恢复目标已存在。请指定一个新的--db路径，避免覆盖数据库。')
            read=configure_sqlite(sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True))
            try:
                if read.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('备份完整性检查失败')
                destination.parent.mkdir(parents=True,exist_ok=True)
                write=configure_sqlite(sqlite3.connect(destination))
                try: read.backup(write)
                finally: write.close()
            finally: read.close()
            output({'restored':str(destination)});return 0
        initialize(args.db)
        config=load_config(args.config)
        if args.command=='init': output({'database':'PostgreSQL / ctn' if is_postgres(args.db) else str(Path(args.db).resolve()),'schema':SCHEMA_VERSION})
        elif args.command=='serve': serve(args.db,config,args.port,args.auto_collect)
        elif args.command=='collect':
            from .collector import collect_once
            if args.max_pages is not None: config['max_pages']=args.max_pages
            if args.counter_limit is not None: config['counter_limit']=args.counter_limit
            if args.no_news: config['collect_news']=False
            if args.no_trending: config['collect_trending']=False
            cycles=0
            while True:
                output(collect_once(args.db,config));cycles+=1
                if args.once or (args.cycles>0 and cycles>=args.cycles): break
                interval=max(30,float(config['interval_seconds']))
                time.sleep(interval)
        elif args.command=='import-activity':
            from .imports import import_activity_csv
            output(import_activity_csv(args.db,Path(args.file).read_text(encoding='utf-8-sig'),Path(args.file).name))
        elif args.command=='import-api-logs':
            from .integrations import import_api_logs
            output(import_api_logs(args.db,Path(args.file).read_text(encoding='utf-8-sig')))
        elif args.command=='import-labels':
            from .integrations import import_labels
            output(import_labels(args.db,Path(args.file).read_text(encoding='utf-8-sig')))
        elif args.command=='record-score':
            from .imports import add_score_snapshot
            output(add_score_snapshot(args.db,args.total,args.at,args.source,args.note))
        elif args.command=='team-accounts':
            if any(x<=0 for x in args.ids) or not args.evidence.strip(): raise ValueError('账号ID必须为正，且需提供归属证据')
            connection=connect(args.db)
            try:
                with connection:
                    connection.executemany('INSERT INTO team_accounts VALUES(?,?,1,?,?) ON CONFLICT(account_id) DO UPDATE SET team_label=excluded.team_label,is_own=1,evidence=excluded.evidence,updated_at=excluded.updated_at',[(x,args.team,args.evidence,utc_now()) for x in set(args.ids)])
            finally: connection.close()
            output({'updated':len(set(args.ids))})
        elif args.command=='analyze':
            from .analytics import analyze
            params={'window_minutes':args.window_minutes};result=analyze(args.db,params)
            connection=connect(args.db)
            try:
                with connection:
                    connection.execute('INSERT INTO analysis_runs(created_at,kind,parameters_json,result_json) VALUES(?,?,?,?)',(utc_now(),'descriptive',json.dumps(params),json.dumps(result,ensure_ascii=False,default=str,allow_nan=False)))
            finally: connection.close()
            if args.output:
                destination=Path(args.output);destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8')
                output({'report':str(destination.resolve())})
            else: output(result)
        elif args.command=='export':
            from .queries import EXPORTS
            from .server import spreadsheet_cell
            destination=Path(args.file);destination.parent.mkdir(parents=True,exist_ok=True)
            connection=connect(args.db)
            try:
                with iter_rows(connection, EXPORTS[args.table]) as cursor, destination.open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream);writer.writerow([col[0] for col in cursor.description])
                    writer.writerows([spreadsheet_cell(value) for value in row] for row in cursor)
            finally: connection.close()
            output({'export':str(destination.resolve())})
        elif args.command=='backup': output({'backup':str(backup(args.db,args.directory))})
        elif args.command=='doctor':
            connection=connect(args.db)
            try:
                if is_postgres(args.db):
                    invalid=connection.execute("SELECT COUNT(*) FROM pg_constraint WHERE connamespace='public'::regnamespace AND NOT convalidated").fetchone()[0]
                    output({'ok':invalid==0,'database':'postgresql','schema_version':SCHEMA_VERSION,
                            'unvalidated_constraints':invalid,
                            'counts':{name:connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('posts','accounts','counter_snapshots','api_call_logs','official_activity')}})
                    return 0 if invalid==0 else 1
                checks=[r[0] for r in connection.execute('PRAGMA integrity_check')]
                fk=[tuple(r) for r in connection.execute('PRAGMA foreign_key_check')]
                output({'ok':checks==['ok'] and not fk,'integrity_check':checks,'foreign_key_violations':fk,'sqlite_version':sqlite3.sqlite_version,
                        'mode':connection.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0],
                        'counts':{name:connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('posts','accounts','counter_snapshots','api_call_logs','official_activity')}})
                return 0 if checks==['ok'] and not fk else 1
            finally: connection.close()
        return 0
    except KeyboardInterrupt:
        print('已停止。',file=sys.stderr);return 130
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 1


if __name__=='__main__': raise SystemExit(main())

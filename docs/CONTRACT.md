# 内部实现合同

Python 3.12+，生产运行仅依赖标准库。包名 ctn_data。Schema v2，日期均使用 ISO8601 UTC；UI 使用 Australia/Sydney。SQLite 放 data/ctn.sqlite3；演示/测试必须单独数据库并标注数据来源。

db.py（主 agent 所有）：connect(path)->sqlite3.Connection（Row factory），initialize(path)，utc_now()，raw_record(conn,source,endpoint,payload,observed_at)->id。下列 schema 是模块间合同。模块使用参数化 SQL。

## 表

- meta(key PK,value)
- raw_records(id PK,sha256 UNIQUE,source,endpoint,observed_at,payload_json)
- accounts(account_id PK,username,display_name,verified,private,created_at,first_seen,last_seen,raw_id)
- team_accounts(account_id PK,team_label,is_own,evidence,updated_at)；无证据账号未知。
- posts(post_id PK,author_id,kind,body,reply_to_post_id,root_post_id,repost_of_post_id,visibility,embed_json,created_at,edited_at,first_seen,last_seen,raw_id)
- post_versions(id PK,post_id,body,edited_at,observed_at,content_hash,raw_id,UNIQUE(post_id,content_hash))
- counter_snapshots(id PK,post_id,observed_at,like_count,reply_count,repost_count,view_count,score_inputs_updated_at,raw_id,UNIQUE(post_id,observed_at))
- feed_observations(id PK,post_id,source,observed_at,rank_position,global_hot_score,UNIQUE(post_id,source,observed_at))
- news_articles(article_key PK,site,slug,title,body,author,published_at,first_seen,last_seen,raw_id)
- api_call_logs(id PK,occurred_at,service,method,path,status,duration_ms,retry_count,retry_after,rate_limit,rate_remaining,rate_reset,error,entity_id)
- collection_runs(id PK,started_at,finished_at,status,metrics_json,error)
- checkpoints(key PK,value,updated_at)
- activity_imports(id PK,sha256 UNIQUE,filename,imported_at,row_count)
- official_activity(id PK,import_id,row_number,occurred_at,event_type,message,fingerprint,occurrence,track,points_decimal,parse_status,parse_note,UNIQUE(fingerprint,occurrence))
- score_snapshots(id PK,observed_at,total_decimal,source,note)
- analysis_runs(id PK,created_at,kind,parameters_json,result_json)
- post_labels(id PK,post_id,label,value,source,version,created_at)

## 子 agent 接口

ingest.py: save_posts(conn,posts,source,observed_at=None,cursor=None)->int; save_counters(conn,post_id,payload,observed_at=None)->int; save_accounts(conn,accounts,observed_at=None)->int; save_news(conn,articles,observed_at=None)->int。不覆盖历史，不把缺失计数写0。

collector.py: collect_once(db_path,config:dict|None=None)->dict；应用默认配置见config.example.json（page_limit:50,max_pages:3,counter_limit:40,counter_new_share:0.5）。新帖基线和旧帖复查分配名额，组内轮转。有排他锁、请求/时间预算、持久断点；锁不绕开限流。返回字段以REPORT_COLLECTION.md及collector.py为准。

transport.py: 官方 allowlist GET；通过 urllib；记录每次尝试状态/耗时/限流；拒绝所有重定向，避免隐藏请求。远程 URL 不接受用户任意输入。参考已核实SDK。

imports.py: import_activity_csv(db_path,content:str,filename='activity.csv')->dict；精确列 When (UTC), Type, Message；跨重叠导出幂等，合法重复事件按出现次数保留；CSV带BOM可读；Decimal；未知日志不伪造积分。 add_score_snapshot(db_path,total,observed_at=None,source='manual',note='')->dict。

scoring.py: rules_context(at=None)->dict；calculate_scenario(payload:dict)->dict；score_overview(db_path)->dict。含完整Red/Blue/Polling公式，日界线为Sydney，倍率按入账；未知字段返回unknown。积分情景计算不得把公开总计数作为官方实得。

analytics.py: analyze(db_path,params:dict|None=None)->dict；返回 summary, timeseries, cohorts, growth, correlation, keywords, network, quality, caveats。没有数据时返回空数组/明确原因，不填演示值。按相同观测窗口比较；缺失观测不当0；相关性不声称因果。提供exportable数据。

## HTTP 合同（主 agent 实现）

GET /api/bootstrap -> {csrf_token,timezone,team_label,db_path,readonly_remote:true}
GET /api/overview -> {counts:{posts,accounts,counter_snapshots,news_articles,api_calls,official_activity},latest_collection,score:<score_overview>,quality,last_observed,mode}
GET /api/posts?q=&author_id=&kind=&own=all|own|unknown&limit=50&offset=0 -> {items,total,limit,offset}，items包含posts所有字段以及latest_counts:{like_count,reply_count,repost_count,view_count,observed_at},team_label,is_own。
GET /api/posts/<id> -> {post,versions,counters,feed_observations,labels,replies,reposts}
GET /api/accounts?q=&limit=&offset= -> {items,total}，含 team_label,is_own,evidence,post_count。
GET /api/news?q=&limit=&offset= -> {items,total}
GET /api/activity?track=&q=&limit=&offset= -> {items,total,imports,score_snapshots}
GET /api/score -> score_overview
GET /api/analysis?window_minutes=60 -> analyze结果
GET /api/operations -> {runs,logs,checkpoints,status_codes,collector_running,config}
GET /api/rules -> rules_context
GET /api/health -> {ok,sqlite_version,schema_version,mode}
GET /api/export?table=posts|counters|accounts|activity|api_logs|news|analysis -> CSV下载

POST必须JSON，带 X-CTN-Token=bootstrap token；仅同源。本地修改端点：
- /api/collect {page_limit?,max_pages?,counter_limit?,collect_news?,collect_trending?} -> 202 {started:true}；后台执行，operations轮询。
- /api/import-activity {csv,filename} -> import结果。
- /api/score-snapshots {total,observed_at?,source?,note?} -> add结果。
- /api/team-accounts {account_ids:[int],team_label,evidence} -> {updated}；is_own=1。
- /api/analysis/run {window_minutes:60} -> 分析结果并存分析快照。
- /api/backup {} -> {filename}（工作区备份）；下载经GET /api/backups/<filename>。

统一错误 {error,detail?}。分页上限200。GET无远程写。每个UI页显示采样范围、未知信息和时间；外部文本用textContent，严禁拼入innerHTML。界面为中文，清楚区分official/observed/scenario，不把unknown渲染成0。CSS/JS本地，无CDN依赖。web/index.html web/app.js web/style.css由看板agent独占。

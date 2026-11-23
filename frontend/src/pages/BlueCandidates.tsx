import {useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {Alert, App, Button, Collapse, Segmented, Skeleton, Table, Tag} from 'antd';
import {CopyOutlined, ExportOutlined} from '@ant-design/icons';
import {request, useAction, useApi, type Row} from '../api';
import {Metrics, QueryState, fmt, time} from '../common';
import './blueCandidates.css';

// Suggestions only: the page never reports anything by itself; the operator confirms each report (D58).
// One card per account (blue_view._accounts): its account-level evidence, its posts with their post reports and,
// once risk_assessment has confirmed a hidden post and 100 actions are collected, an account report.
const skipReasons:Record<string,string>={already_reported:'今天已经举报过',verified_or_unknown:'账号已认证或认证状态未知',non_positive_ev:'预期得分不为正',cluster_daily_cap:'同一簇今天已满额',style_group_daily_cap:'同一文风组今天已满额',possible_teammate:'可能是未登记的队友',author_daily_cap:'同一作者今天已满额'};
const stopReasons:Record<string,string>={below_threshold:'后面的候选可信度低于阈值，没有列入',exhausted:'候选已全部看完'};
const accountMissing:Record<string,string>={below_threshold:'可信度低于阈值',verified_or_unknown:'账号已认证或认证状态未知',hidden:'还没有确认隐藏的帖子',suspension_line:'已到 3 条封号线，可能已被封',actions:'采集到的动作不到 100 次',recently_reported:'6 小时内已经举报过，等裁定',cluster_daily_cap:'同一簇今天的全额名额已用完，先报帖子',style_group_daily_cap:'同一文风组今天的全额名额已用完，先报帖子',possible_teammate:'与我方文风同组，可能是未登记的队友',no_note:'账号层面的证据不足以生成说明'};
const percent=(value:unknown)=>Number.isFinite(Number(value))?`${Math.round(Number(value)*100)}%`:'—';
// Links from the account clusters page carry the Blue cluster ids of a cluster's members: /blue?clusters=c1,a5.
export function clusterFilter(search:URLSearchParams):string[] {return [...new Set((search.get('clusters')||'').split(',').map(id=>id.trim()).filter(Boolean))];}
// Blue's 24-hour text clusters are not the account clusters; the clusters page finds the one holding this id.
export const accountClusterLink=(clusterId:unknown)=>`/analysis/clusters?blue_cluster=${encodeURIComponent(String(clusterId))}`;
// Referendum side of the candidate's cluster, estimated from its posts (blue_reports._sides); Yes is our side.
const sides:Record<string,{label:string,color?:string}>={no:{label:'No 方',color:'volcano'},yes:{label:'Yes 方（我方）',color:'green'},mixed:{label:'立场混合'},unknown:{label:'立场不明'}};
export const sideFilter=(search:URLSearchParams)=>search.get('side')==='no'?'no':'';
// Evidence about one post rather than its author stays on the post (blue_view.POST_ONLY_RULES).
const postOnlyRules=new Set(['shared_text_post','cotimed_post','chain_text','deep_post','answer_latency']);
const isAccountReport=(row:Row)=>row.target==='account';
// Accounts per referendum side, mixed and unknown together; Yes is our side (D49), so the counts stay apart.
const bySide=(rows:Row[])=>{const count=(test:(side:unknown)=>boolean)=>rows.filter(row=>test(row.side)).length;
 return `No 方 ${count(side=>side==='no')} · 立场不明/混合 ${count(side=>side!=='no'&&side!=='yes')} · Yes 方（我方）${count(side=>side==='yes')}`;};

type AccountGroup={account_id:number;posts:Row[];info?:Row};
// Posts grouped by author in their listed order; info is the server's account row (evidence, hidden posts, account report).
// Cards follow the server's account order (blue_view._accounts: Yes side last, then hidden posts and actions).
export function groupByAccount(posts:Row[],infos:Map<number,Row>,extra:Row[]=[]):AccountGroup[] {
 const groups=new Map<number,AccountGroup>(),order=new Map([...infos.keys()].map((id,index)=>[id,index]));
 for(const post of posts){const id=Number(post.author_id);let group=groups.get(id);if(!group){group={account_id:id,posts:[],info:infos.get(id)};groups.set(id,group);}group.posts.push(post);}
 for(const info of extra)if(!groups.has(Number(info.account_id)))groups.set(Number(info.account_id),{account_id:Number(info.account_id),posts:[],info});
 return [...groups.values()].sort((a,b)=>(order.get(a.account_id)??Infinity)-(order.get(b.account_id)??Infinity));
}

// The strongest item per rule across the account's posts, when the server sent no account row.
function accountEvidence(posts:Row[]):Row[] {
 const best=new Map<string,Row>();
 for(const item of posts.flatMap(post=>post.evidence||[]))if(!postOnlyRules.has(item.rule)&&(!best.has(item.rule)||Number(item.log_odds||0)>Number(best.get(item.rule)?.log_odds||0)))best.set(item.rule,item);
 return [...best.values()];
}

// Copy evidence (blue_view._attach_accounts, D99/D100): copy_patterns' shared wording, with style_groups' group under
// 'style'. Both have a 'peers' list meaning different things, so they are shown apart.
function CopyEvidence({value}:{value?:Row|null}) {
 if(!value)return null;
 const phrases:Row[]=value.phrases||[],peers:unknown[]=value.peers||[],style:Row|null=value.style||null,examples:Row[]=style?.examples||[];
 const wording=value.template_share!==undefined&&value.template_share!==null;
 if(!wording&&!style?.style_group)return null;
 return <div className="blue-copy">
  {wording&&<><div className="muted">文案：模板帖占比 {percent(value.template_share)} · 共用短语账号 {fmt(peers.length)} 个{value.copied_facts?` · 照抄事实 ${fmt(value.copied_facts)} 条`:''}</div>
   {phrases.length>0&&<ul>{phrases.slice(0,3).map((phrase,index)=><li key={index}>“{phrase.phrase}” · {fmt(phrase.accounts)} 个账号{phrase.npc_free===false&&' · NPC 也用'}</li>)}</ul>}</>}
  {style?.style_group&&<><div className="muted">文风：同组 {fmt(style.group_size)} 人（其中被隐藏 {fmt(style.group_hidden_accounts)} 个） · 组 {style.style_group}</div>
   {examples.length>0&&<ul>{examples.slice(0,2).map(example=><li key={example.post_id}><Link to={`/posts?post=${example.post_id}`}>@{example.username||example.account_id}</Link>：{example.excerpt}</li>)}</ul>}</>}
 </div>;
}

function Evidence({items}:{items:Row[]}) {
 const sorted=[...(items||[])].sort((a,b)=>Number(b.log_odds||0)-Number(a.log_odds||0));
 const shown=sorted.slice(0,4),rest=sorted.slice(4);
 if(!sorted.length)return null;
 return <div className="blue-evidence">
  <ul>{shown.map((item,index)=><li key={index}>{item.detail||item.rule}</li>)}</ul>
  {rest.length>0&&<details><summary>另外 {rest.length} 条依据</summary><ul>{rest.map((item,index)=><li key={index}>{item.detail||item.rule}</li>)}</ul></details>}
 </div>;
}

// A person files each report: the button opens a dialog with the exact report, and only its OK sends it (D58).
function ReportButton({item,report}:{item:Row;report?:Row}) {
 const {message,modal}=App.useApp(),submit=useAction('/blue/report'),client=useQueryClient();
 if(report?.status==='submitted')return <Tag color="green">已举报 · {time(report.at)}</Tag>;
 function open() {
  modal.confirm({title:`用 Simon 的账号举报帖子 #${item.post_id}？`,okText:'确认提交',cancelText:'取消',okButtonProps:{danger:true},
   content:<div className="blue-confirm"><p>作者 @{item.username||item.author_id}（账号 #{item.author_id}） · 可信度 {percent(item.p)} · 簇 {item.cluster_id}</p><p>举报原因：inauthentic（{item.suggested_reason}）</p><blockquote>{item.suggested_note}</blockquote><p className="muted">提交后不能撤回；如果被判为真人，全队暂停举报 6 小时。</p></div>,
   onOk:async()=>{
    try {await submit.mutateAsync({post_id:item.post_id,confirmed:true});void message.success(`已提交举报 #${item.post_id}`);}
    catch(error) {
     void message.error((error as Error).message||'举报失败');
     await Promise.all(['/blue/reports','/blue/reports/history','/blue/candidates'].map(path=>client.invalidateQueries({queryKey:[path]})));
    }
   }});
 }
 return <>{report?.status==='failed'&&<Tag color="red">上次失败：{report.error||'—'}</Tag>}<Button danger onClick={open} disabled={!item.suggested_note}>用 Simon 的账号举报</Button></>;
}

// The same for an account report: only a ready account (blue_view) gets the button; the dialog shows the exact note.
function AccountReportButton({account,report}:{account:Row;report?:Row}) {
 const {message,modal}=App.useApp(),submit=useAction('/blue/report-account'),client=useQueryClient(),ready:Row=account.account_report||{};
 if(report?.status==='submitted')return <Tag color="green">已举报账号 · {time(report.at)}</Tag>;
 function open() {
  modal.confirm({title:`用 Simon 的账号举报账号 @${account.username||account.account_id}（#${account.account_id}）？`,okText:'确认提交',cancelText:'取消',okButtonProps:{danger:true},
   content:<div className="blue-confirm"><p>已确认隐藏 {fmt(ready.hidden)} 条 · 采集到的动作 {fmt(ready.actions)} 次 · 可信度 {percent(account.p)} · 簇 {account.cluster_id}</p><p>举报原因：inauthentic（{ready.suggested_reason}）</p><blockquote>{ready.suggested_note}</blockquote><p className="muted">提交后不能撤回。裁定时不满足「已有 1 条帖子被版主隐藏、平台动作满 100 次」会被驳回：不得分，也不罚停。</p></div>,
   onOk:async()=>{
    try {await submit.mutateAsync({account_id:account.account_id,confirmed:true});void message.success(`已提交账号举报 #${account.account_id}`);}
    catch(error) {
     void message.error((error as Error).message||'举报失败');
     await Promise.all(['/blue/reports','/blue/reports/history','/blue/candidates'].map(path=>client.invalidateQueries({queryKey:[path]})));
    }
   }});
 }
 return <>{report?.status==='failed'&&<Tag color="red">上次失败：{report.error||'—'}</Tag>}<Button danger onClick={open} disabled={!ready.eligible||!ready.suggested_note}>用 Simon 的账号举报这个账号</Button></>;
}

function RecheckButton({item}:{item:Row}) {
 const {message}=App.useApp(),client=useQueryClient(),[error,setError]=useState('');
 const recheck=useMutation<Row,Error>({mutationFn:async()=>{
  const bootstrap=await client.fetchQuery({queryKey:['bootstrap-token'],queryFn:()=>request('/bootstrap'),staleTime:0});
  return request('/blue/recheck',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':bootstrap.csrf_token},body:JSON.stringify({post_id:item.post_id,limit:50})});
 }});
 async function run() {
  setError('');
  try {
   const result=await recheck.mutateAsync();
   if(result.view){await client.cancelQueries({queryKey:['/blue/candidates']});client.setQueryData(['/blue/candidates',{limit:50}],result.view);}
   else await client.invalidateQueries({queryKey:['/blue/candidates']});
   void message.info(result.availability?.status==='available'?'已确认可访问':result.availability?.status==='unavailable'?'仍无法访问':'仍未确认');
  }catch(reason){const detail=(reason as Error).message||'复核失败';setError(detail);void message.error(detail);await client.invalidateQueries({queryKey:['/blue/candidates']});}
 }
 return <>{error&&<Tag color="red">复核失败：{error}</Tag>}<Button loading={recheck.isPending} onClick={()=>void run()}>再次确认</Button></>;
}

function availabilityProblem(availability:Row) {
 const status=Number(availability.http_status),prefix=status?`HTTP ${status} · `:'';
 const detail=status===404||status===410?'当前无法读取，可能隐藏或删除':status===403?'当前没有读取权限':status===429?'读取额度受限，稍后再确认':availability.status==='unverified'?'暂未确认可访问，可能是网络或平台暂时异常':'当前无法读取，原因待核对';
 return prefix+detail;
}

// One post; inside an account card the account's name, evidence and cluster sit on the card instead.
function CandidateCard({item,report,reportable=false,availabilityIssue=false,inAccount=false}:{item:Row;report?:Row;reportable?:boolean;availabilityIssue?:boolean;inAccount?:boolean}) {
 const {message}=App.useApp();
 const checkedAt=item.availability_checked_at||item.availability?.checked_at;
 async function copy() {
  try {await navigator.clipboard.writeText(String(item.suggested_note));void message.success('已复制举报说明');}
  catch {void message.error('复制失败，请手动选中说明文字');}
 }
 return <article className={inAccount?'blue-card blue-post':'blue-card'}>
  <header>
   {inAccount
    ?<div className="blue-card-title">{item.plan_rank&&<span className="blue-rank">#{item.plan_rank}</span>}<span className="muted">帖子 #{item.post_id} · {time(item.created_at)}</span></div>
    :<div className="blue-card-title">{item.plan_rank&&<span className="blue-rank">#{item.plan_rank}</span>}<strong>@{item.username||item.author_id}</strong><span className="muted">账号 #{item.author_id} · {time(item.created_at)}</span></div>}
   <div className="blue-card-scores"><Tag>可信度 {percent(item.p)}</Tag><Tag>预期 {fmt(item.ev)} 分</Tag>{!inAccount&&<Tag>簇 {item.cluster_id} · {fmt(item.cluster_size)} 个账号</Tag>}{!inAccount&&item.side&&<Tag color={sides[item.side]?.color} title={`簇内帖子：No ${fmt(item.side_counts?.no??0)} 条 · Yes ${fmt(item.side_counts?.yes??0)} 条`}>{sides[item.side]?.label||item.side}</Tag>}{item.slot_minute!==undefined&&item.slot_minute!==null&&<Tag>第 {Number(item.slot_minute)+1} 批</Tag>}</div>
  </header>
  <p className="blue-excerpt">{item.excerpt||'无文本内容'}</p>
  {availabilityIssue&&<Alert type="warning" showIcon title={availabilityProblem(item.availability||{})} description={item.availability?.error||'尚未确认当前访问状态；再次确认前暂不能举报。'}/>}
  {checkedAt&&<div className="muted">平台核验于 {time(checkedAt)}</div>}
  <Evidence items={inAccount?(item.evidence||[]).filter((evidence:Row)=>postOnlyRules.has(evidence.rule)):item.evidence||[]}/>
  <div className="blue-report">
   <div className="muted">举报类别：{item.suggested_reason||'—'}</div>
   {item.suggested_note?<blockquote>{item.suggested_note}</blockquote>:<p className="muted">没有足够强的证据生成说明，建议先在本地帖子详情里核对。</p>}
  </div>
  <div className="toolbar">
   {availabilityIssue?<RecheckButton item={item}/>:reportable&&<ReportButton item={item} report={report}/>}
   {item.suggested_note&&<Button icon={<CopyOutlined/>} onClick={()=>void copy()}>复制说明</Button>}
   {item.platform_url&&<Button href={item.platform_url} target="_blank" rel="noopener noreferrer" icon={<ExportOutlined/>} iconPlacement="end">在平台打开 #{item.post_id}</Button>}
   <Link to={item.local_url||`/posts?post=${item.post_id}`}>本地帖子详情</Link>
   {!inAccount&&item.cluster_id&&<Link to={accountClusterLink(item.cluster_id)}>所属账号簇</Link>}
  </div>
 </article>;
}

function AccountCard({group,reports,reportable=false}:{group:AccountGroup;reports:Row[];reportable?:boolean}) {
 const info=group.info,first:Row=group.posts[0]||{},account:Row=info||{account_id:group.account_id,username:first.username,p:first.p,cluster_id:first.cluster_id,cluster_size:first.cluster_size,side:first.side,side_counts:first.side_counts};
 const best=Math.max(Number(account.p||0),...group.posts.map(post=>Number(post.p||0))),ready:Row|undefined=info?.account_report;
 const rank=group.posts.map(post=>Number(post.plan_rank)).filter(Number.isFinite).sort((a,b)=>a-b)[0];
 const accountReport=reports.find(row=>isAccountReport(row)&&Number(row.author_id)===group.account_id);
 const name=account.username||first.username;
 return <section className="blue-account" aria-label={`账号 @${name||group.account_id}`}>
  <header>
   <div className="blue-card-title">{rank!==undefined&&<span className="blue-rank">#{rank}</span>}<strong>@{name||group.account_id}</strong><span className="muted">账号 #{group.account_id}</span></div>
   <div className="blue-card-scores">
    <Tag>可信度 {percent(best)}</Tag>
    <Tag>簇 {account.cluster_id} · {fmt(account.cluster_size)} 个账号</Tag>
    {account.side&&<Tag color={sides[account.side]?.color} title={`簇内帖子：No ${fmt(account.side_counts?.no??0)} 条 · Yes ${fmt(account.side_counts?.yes??0)} 条`}>{sides[account.side]?.label||account.side}</Tag>}
    {ready&&<Tag color={ready.hidden?'red':undefined}>已确认隐藏 {fmt(ready.hidden)} 条{ready.suspect?` · 待复核 ${fmt(ready.suspect)} 条`:''}</Tag>}
    {ready&&<Tag>采集到的动作 {fmt(ready.actions)} 次</Tag>}
   </div>
  </header>
  <Evidence items={info?.evidence||accountEvidence(group.posts)}/>
  <CopyEvidence value={info?.copy_evidence}/>
  {info?.teammate_note&&<Alert type="info" showIcon title={info.teammate_note} description="已从候选、今日建议和举报按钮里剔除；队友登记不改。"/>}
  {!info?.teammate_note&&Number(info?.linked_to_ours||0)>0&&<Alert type="warning" showIcon title={`与我方 ${fmt(info?.linked_to_ours)} 个账号文风相近，举报前请核对`}/>}
  {ready&&<div className="blue-report">
   <div className="muted">账号举报：{ready.eligible?'满足条件（已有确认隐藏的帖子，动作满 100 次）':`还不能举报 — ${(ready.missing||[]).map((key:string)=>accountMissing[key]||key).join('；')}`}</div>
   {ready.eligible&&ready.suggested_note&&<blockquote>{ready.suggested_note}</blockquote>}
   {((ready.eligible&&reportable)||accountReport)&&<div className="toolbar"><AccountReportButton account={account} report={accountReport}/></div>}
  </div>}
  {group.posts.length>0&&<div className="blue-account-posts">{group.posts.map(post=><CandidateCard key={post.post_id} item={post} inAccount reportable={reportable&&!!post.plan_rank}
   report={reports.find(row=>!isAccountReport(row)&&row.post_id===post.post_id)}/>)}</div>}
  <div className="toolbar"><Link to={`/accounts?q=${group.account_id}`}>账号详情</Link>{account.cluster_id&&<Link to={accountClusterLink(account.cluster_id)}>所属账号簇</Link>}<Link to="/processing/risk">风险评估</Link></div>
 </section>;
}

const reportColumns=(dateTitle:string)=>[
 {title:dateTitle,dataIndex:'at',render:(value:string)=>time(value)},
 {title:'帖子',dataIndex:'post_id',render:(id:number,row:Row)=>isAccountReport(row)?'—':<Link to={`/posts?post=${id}`}>#{id}</Link>},
 {title:'对象',dataIndex:'target',render:(_:unknown,row:Row)=>isAccountReport(row)?<Tag color="purple">账号</Tag>:'帖子'},
 {title:'作者',dataIndex:'username',render:(name:string,row:Row)=>name?`@${name}`:`#${row.author_id}`},
 {title:'簇',dataIndex:'cluster_id'},
 {title:'结果',dataIndex:'status',render:(status:string,row:Row)=>status==='submitted'?<Tag color="green">已提交</Tag>:<Tag color="red">失败：{row.error||'—'}</Tag>}];

export default function BlueCandidates() {
 // The server caches each result for five minutes and recomputes in the background.
 const query=useApi<Row>('/blue/candidates',{limit:50},300000,true),[search,setSearch]=useSearchParams(),only=clusterFilter(search),side=sideFilter(search);
 // Today's report attempts, newest first; the latest one per post or account marks its card.
 const reports:Row[]=useApi<Row>('/blue/reports',{},60000,true).data?.reports||[];
 const [historyOpen,setHistoryOpen]=useState(false),[historyOffset,setHistoryOffset]=useState(0);
 const history=useQuery<Row,Error>({queryKey:['/blue/reports/history',{limit:20,offset:historyOffset}],queryFn:({signal})=>request(`/blue/reports/history?limit=20&offset=${historyOffset}`,{signal}),enabled:historyOpen});
 function setSide(value:string) {const next=new URLSearchParams(search);if(value==='no')next.set('side','no');else next.delete('side');setSearch(next);}
 if(query.isPending)return <div className="stack"><Alert type="info" showIcon title="正在计算候选" description="服务刚启动时第一次计算大约需要 10 秒。"/><Skeleton active paragraph={{rows:8}}/></div>;
 return <QueryState query={query.data?{...query,isError:false}:query}>{data=>{
  if(data.submits_reports!==false)return <Alert type="error" showIcon title="接口没有确认只读模式，页面不显示候选" description="这个页面只能展示建议。请联系负责数据接口的会话核对 /api/blue/candidates。"/>;
  const submitted=new Set([...reports,...(history.data?.reports||[])].filter(row=>row.status==='submitted'&&!isAccountReport(row)).map(row=>row.post_id));
  const plan=data.plan||{},planned:Row[]=(plan.items||[]).filter((row:Row)=>!submitted.has(row.post_id)),all:Row[]=(data.candidates||[]).filter((row:Row)=>!submitted.has(row.post_id)),passed:Row[]=plan.skipped||[],issues:Row[]=(data.availability_issues||[]).filter((row:Row)=>!submitted.has(row.post_id));
  // The metrics stay those of the whole day's plan; the filter only narrows the lists below them.
  // Skipped rows carry no side, so it is looked up from the candidate with the same post.
  const sideOf=new Map([...all,...issues].map(row=>[row.post_id,row.side])),noCount=[...all,...issues].filter(row=>row.side==='no').length;
  const keep=(row:Row)=>(!only.length||only.includes(String(row.cluster_id)))&&(!side||(row.side??sideOf.get(row.post_id))===side),items=planned.filter(keep),candidates=all.filter(keep),skipped=passed.filter(keep),pending=issues.filter(keep);
  const infos=new Map<number,Row>((data.accounts||[]).map((row:Row)=>[Number(row.account_id),row]));
  // Today's suggestions: accounts with planned posts, then accounts whose account report alone is ready.
  const readyOnly=(data.accounts||[]).filter((row:Row)=>row.account_report?.eligible&&keep(row));
  const today=groupByAccount(items,infos,readyOnly).map(group=>group.posts.length?group:{...group,posts:candidates.filter(post=>Number(post.author_id)===group.account_id)});
  const ready:Row[]=(data.accounts||[]).filter((row:Row)=>row.account_report?.eligible),teammates:Row[]=(data.excluded_teammates||[]).filter(keep);
  const empty=pending.length?'当前没有可举报的建议；访问问题见下方待核对建议。':`${only.length?'这些簇今天':'今天'}没有达到阈值的${side?' No 方':''}候选。可以展开下面的可访问候选自行核对。`;
  return <div className="stack">
   {query.isError&&<Alert type="warning" showIcon title="候选刷新失败，保留上次结果" description={query.error?.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>}
   <Alert type="warning" showIcon title="每条举报都要你在这里确认后，才会用 Simon 的账号提交" description={<ul className="blue-caveats">{(data.caveats||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>}/>
   <div className="toolbar"><span className="muted">计算于 {time(data.generated_at)} · 悉尼日期 {data.day||'—'}</span>{data.stale&&<Tag color="processing">更新中</Tag>}<Segmented aria-label="按立场筛选" value={side||'all'} onChange={value=>setSide(String(value))} options={[{label:'全部立场',value:'all'},{label:`只看 No 方 · ${noCount}`,value:'no'}]}/><Link to="/processing/risk">风险评估（隐藏帖）</Link><Link to="/ai/analysis?ai_view=screening">账号筛查（本地详细检查）</Link></div>
   <Metrics items={[{label:'今日建议举报',value:planned.length,hint:stopReasons[plan.stop_reason]||plan.stop_reason},{label:'可举报账号',value:ready.length,hint:<>{bySide(ready)}<br/>已确认隐藏 ≥1 条，动作 ≥100 次</>},{label:'预计得分',value:plan.expected_value},{label:'候选总数',value:all.length+issues.length,hint:`可信度阈值 ${percent(data.threshold)}`},{label:'未列入建议',value:passed.length}]}/>
   {only.length>0&&<Alert type="info" showIcon title={`只显示 Blue 簇 ${only.join('、')} 的候选`} description={`来自账号簇页：今日建议 ${items.length} 条，全部候选 ${candidates.length+pending.length} 条。上面的数字仍是全天的计划。`} action={<span className="blue-filter-links"><Link to="/blue">显示全部候选</Link><Link to={accountClusterLink(only[0])}>回到账号簇</Link></span>}/>}
   <section className="stack"><h2>今日建议</h2>{today.length?<><p className="muted">{today.length} 个账号（{bySide(today.map(group=>group.info||group.posts[0]||{}))}）；每个账号先报帖子，满足条件后再举报账号。</p>{today.map(group=><AccountCard key={group.account_id} group={group} reports={reports} reportable/>)}</>:<p className="muted">{empty}</p>}</section>
   {pending.length>0&&<section className="stack"><h2>待核对建议 · {pending.length}</h2>{pending.map(item=><CandidateCard key={item.post_id} item={item} availabilityIssue/>)}</section>}
   <Collapse onChange={keys=>setHistoryOpen((Array.isArray(keys)?keys:[keys]).includes('history'))} items={[
    {key:'reports',label:`今天的举报记录 · ${reports.length}`,children:<Table<Row> size="small" rowKey={row=>`${row.target||'post'}-${row.post_id??row.author_id}-${row.at}`} pagination={false} dataSource={reports} columns={reportColumns('时间')}/>},
    {key:'history',label:'历史举报记录',children:<QueryState query={history}>{records=><div className="stack"><p className="muted">按提交时间倒序；已提交仅表示举报请求已提交，不代表举报已裁定成立。</p><Table<Row> size="small" rowKey={row=>`${row.target||'post'}-${row.post_id??row.author_id}-${row.at}`} dataSource={records.reports||[]} pagination={{current:historyOffset/20+1,pageSize:20,total:records.total||0,showSizeChanger:false,hideOnSinglePage:true,onChange:page=>setHistoryOffset((page-1)*20)}} columns={reportColumns('日期时间')}/></div>}</QueryState>},
    {key:'skipped',label:`未列入今日建议 · ${skipped.length}`,children:<Table<Row> size="small" rowKey="post_id" pagination={false} dataSource={skipped} columns={[{title:'帖子',dataIndex:'post_id',render:(id:number)=><Link to={`/posts?post=${id}`}>#{id}</Link>},{title:'簇',dataIndex:'cluster_id'},{title:'原因',dataIndex:'reason',render:(reason:string)=>skipReasons[reason]||reason}]}/>},
    {key:'all',label:`可访问候选 · ${candidates.length}`,children:<div className="stack">{groupByAccount(candidates,infos).map(group=><AccountCard key={group.account_id} group={group} reports={reports}/>)}</div>},
    // Probable unregistered teammates: shown apart with the reason, never reportable (style_groups possible_teammate).
    ...(teammates.length?[{key:'teammates',label:`可能是未登记的队友，已剔除 · ${teammates.length}`,children:<div className="stack">{teammates.map(row=><AccountCard key={row.account_id} group={{account_id:Number(row.account_id),posts:row.posts||[],info:row}} reports={reports}/>)}</div>}]:[])
   ]}/>
  </div>;
 }}</QueryState>;
}

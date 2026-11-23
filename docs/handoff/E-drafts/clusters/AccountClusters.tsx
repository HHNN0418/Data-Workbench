import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Segmented, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {QueryState, fmt, time} from '../common';
import {IdentityTags} from '../ui/status';

// Account clusters (D41): behaviour-based groups of accounts, with our own accounts pinned first as the baseline.
export const ruleLabels:Record<string,string>={behaviour_bot_rule:'行为判定为机器人',deep_threads:'常回复很深的对话',fast_answers:'回复非常快',quick_answers:'回复较快',shared_text_post:'帖子和其他账号文本相同',shared_text_author:'和其他账号发过相同文本',chain_text:'文本串联',cotimed_post:'和其他账号同时发帖',cotimed_author:'发帖时间和其他账号同步',self_repeat_strong:'大量重复自己的文本',self_repeat:'重复自己的文本',deep_post:'回复在深层对话里'};
const sorts={engagement:'互动分 / 时',size:'账号数',bots:'机器人比例'} as const;
type SortKey=keyof typeof sorts;
const pct=(value:unknown)=>Number.isFinite(Number(value))&&value!==null?`${Math.round(Number(value)*100)}%`:'—';

export function orderClusters(rows:Row[],sort:SortKey):Row[] {
 const value=(row:Row)=>Number(sort==='size'?row.size:sort==='bots'?row.bot_share:row.engagement_per_hour)||0;
 return [...rows].sort((a,b)=>Number(!!b.is_own)-Number(!!a.is_own)||value(b)-value(a));
}
function clusterName(row:Row) {return row.is_own?'我们':row.label||`簇 ${row.cluster_id}`;}
function stanceSummary(counts:Row={}) {
 const yes=Number(counts.yes)||0,no=Number(counts.no)||0,undecided=Number(counts.undecided)||0,all=yes+no+undecided+(Number(counts.unrelated)||0);
 return all?`Yes ${pct(yes+no?yes/(yes+no):null)} · 犹豫 ${pct(undecided/all)}`:'—';
}

function ClusterDetail({row}:{row:Row}) {
 const members:Row[]=row.members||[],posts:Row[]=row.top_posts||[],reasons:Row[]=row.reasons||[];
 return <div className="cluster-detail">
  <section><h3>成员账号 · {fmt(row.members_total??members.length)}</h3><ul>{members.map(member=><li key={member.author_id}><Link to={`/accounts?q=${member.author_id}`}>@{member.username||member.author_id}</Link><span className="muted">#{member.author_id}{member.posts!=null?` · ${fmt(member.posts)} 帖`:''}</span><IdentityTags isBot={member.is_bot} botReasons={member.bot_reasons}/></li>)}</ul>{row.members_total>members.length&&<p className="muted">只显示前 {members.length} 个账号。</p>}</section>
  <section><h3>代表帖子</h3>{posts.length?<ul>{posts.map(post=><li key={post.post_id}><Link to={`/posts?post=${post.post_id}`}>#{post.post_id}</Link><span className="cluster-excerpt">{post.excerpt||'无文本内容'}</span>{post.points!=null&&<span className="muted">互动分 {fmt(post.points)}</span>}</li>)}</ul>:<p className="muted">没有代表帖子。</p>}</section>
  {!row.is_own&&<section><h3>机器人判定原因</h3>{reasons.length?<ul>{reasons.map(reason=><li key={reason.rule}>{ruleLabels[reason.rule]||reason.rule}<span className="muted"> · {fmt(reason.count)} 次</span></li>)}</ul>:<p className="muted">这一簇没有命中机器人规则。</p>}
   {Number(row.blue_candidates)>0&&<Link to={`/blue?cluster=${encodeURIComponent(row.cluster_id)}`}>查看这一簇的 {fmt(row.blue_candidates)} 条举报候选</Link>}</section>}
 </div>;
}

export default function AccountClusters() {
 const [search,setSearch]=useSearchParams(),selected=search.get('cluster'),sort=(search.get('sort') as SortKey)||'engagement';
 const query=useApi<Row>('/clusters',{hours:72},300000,true);
 const update=(key:string,value:string|null)=>setSearch(previous=>{const next=new URLSearchParams(previous);value?next.set(key,value):next.delete(key);return next;},{replace:true});
 return <div className="stack">
  <Alert type="info" showIcon title="簇是按行为聚出来的账号组，不等于已确认的队伍" description="同一簇里的账号在发帖时间、文本或互动方式上很相似。可以用来看竞争对手的规模和打法；举报仍要逐条核对证据。"/>
  <QueryState query={query}>{data=>{
   const rows=orderClusters(data.clusters||[],sorts[sort]?sort:'engagement');
   return <div className="stack">
    <div className="toolbar"><Segmented aria-label="排序" value={sorts[sort]?sort:'engagement'} onChange={value=>update('sort',value==='engagement'?null:String(value))} options={Object.entries(sorts).map(([value,label])=>({value,label}))}/><span className="muted">最近 {fmt(data.window_hours)} 小时 · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Table<Row> rowKey={row=>String(row.cluster_id)} dataSource={rows} pagination={false} scroll={{x:720}}
     rowClassName={row=>row.is_own?'cluster-own':''}
     expandable={{expandedRowKeys:selected?[selected]:[],onExpand:(open,row)=>update('cluster',open?String(row.cluster_id):null),expandedRowRender:row=><ClusterDetail row={row}/>}}
     columns={[
      {title:'簇',key:'name',render:(_,row)=><span>{clusterName(row)}{row.is_own&&<Tag className="cluster-own-tag">对照</Tag>}</span>},
      {title:'账号数',dataIndex:'size',align:'right',render:fmt},
      {title:'互动分 / 时',dataIndex:'engagement_per_hour',align:'right',render:fmt},
      {title:'发帖 / 时',dataIndex:'posts_per_hour',align:'right',render:fmt},
      {title:'立场',key:'stance',render:(_,row)=>stanceSummary(row.stance_counts)},
      {title:'疑似机器人',dataIndex:'bot_share',align:'right',render:pct},
      {title:'Blue 候选',dataIndex:'blue_candidates',align:'right',render:(value,row)=>row.is_own?'—':Number(value)>0?<Link to={`/blue?cluster=${encodeURIComponent(row.cluster_id)}`}>{fmt(value)}</Link>:'0'}
     ]}/>
    {!!data.caveats?.length&&<ul className="muted">{data.caveats.map((text:string)=><li key={text}>{text}</li>)}</ul>}
   </div>;
  }}</QueryState>
 </div>;
}

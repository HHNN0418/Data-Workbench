import {useEffect, useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Button, Skeleton, Table, Tag} from 'antd';
import type {SortOrder, SorterResult} from 'antd/es/table/interface';
import {useApi, type Row} from '../api';
import {QueryState, time} from '../common';
import {IdentityTags} from '../ui/status';
import {Advanced} from '../ui/layout';
import {n, num, pct} from './pipelineModel';
import './clusters.css';

// Account clusters (D41, docs/handoff/C-api.md §2): behaviour-based account groups, with our own accounts as the
// baseline row. Cluster ids change on every recompute and are not Blue's cluster ids.
export const clusterNote='簇是按行为聚出来的账号组，不等于已确认的队伍';
export const botReasonLabels:Record<string,string>={behavior:'行为规则命中',always_on:'长时间持续活跃',high_volume:'写入量很大',fast_reply:'回复非常快',regular_rhythm:'发帖节奏过于规律',duplicate_text:'和其他账号文本相同'};
const exampleLabels:Record<string,string>={shared_text:'同文',citizen_replies:'市民回复最多',original:'普通原创'};
const sortFields={points:'citizen_points_per_hour',accounts:'accounts',writes:'writes',bots:'bot_share'} as const;
export type SortKey=keyof typeof sortFields;
const isSortKey=(value:unknown):value is SortKey=>typeof value==='string'&&value in sortFields;

// Our row first whatever the order; missing values last in both directions.
export function orderClusters(own:Row|null|undefined,rows:Row[],sort:SortKey='points',ascending=false):Row[] {
 const field=sortFields[sort];
 const sorted=[...rows].filter(row=>!row.is_own).sort((a,b)=>{
  const x=num(a[field]),y=num(b[field]);
  if(x===null||y===null)return x===null&&y===null?0:x===null?1:-1;
  return ascending?x-y:y-x;
 });
 return own?[{...own,is_own:true},...sorted]:sorted;
}
// The cluster holding a Blue candidate's cluster, for links from the Blue page.
export function clusterForBlue(rows:Row[],blueCluster:string|null):Row|undefined {
 return blueCluster?rows.find(row=>(row.blue_cluster_ids||[]).map(String).includes(blueCluster)):undefined;
}
export const blueLink=(row:Row)=>`/blue?clusters=${(row.blue_cluster_ids||[]).map((id:unknown)=>encodeURIComponent(String(id))).join(',')}`;
function clusterName(row:Row) {return row.is_own?row.label||'我们自己（Agent Zero）':`簇 ${row.cluster_id}`;}
function stance(row:Row) {
 const value=row.stance||{};
 return num(row.stance_posts)?`Yes ${pct(value.yes)} · No ${pct(value.no)} · 犹豫 ${pct(value.undecided)}`:'—';
}

// 48 hourly bars, oldest on the left: shows round-the-clock activity at a glance.
function HourlyBars({values}:{values:unknown[]}) {
 const counts=values.map(value=>num(value)||0),top=Math.max(1,...counts);
 return <svg className="cluster-hours" viewBox={`0 0 ${counts.length*4} 32`} preserveAspectRatio="none" role="img" aria-label={`${counts.length} 小时写入，最多的一小时 ${n(Math.max(0,...counts))} 条`}>
  {counts.map((count,index)=><rect key={index} x={index*4} y={32-Math.max(count?1:0,count/top*32)} width={3} height={Math.max(count?1:0,count/top*32)}/>)}
 </svg>;
}

const MEMBERS_SHOWN=12;
function ClusterDetail({row,hours}:{row:Row;hours:number}) {
 const [all,setAll]=useState(false);
 const members:Row[]=row.members||[],shown=all?members:members.slice(0,MEMBERS_SHOWN),examples:Row[]=row.examples||[];
 const reasons=Object.entries((row.bot_reasons||{}) as Record<string,unknown>).sort((a,b)=>(num(b[1])||0)-(num(a[1])||0));
 const kinds=row.kinds||{};
 return <div className="cluster-detail">
  <section>
   <h3>写入 · {n(row.writes)} 条 / {n(hours)} 小时</h3>
   <p className="muted">原创 {n(kinds.post)} · 回复 {n(kinds.reply)} · 转发 {n(kinds.repost)}；原创 ÷ 回复 {n(row.post_reply_ratio)}</p>
   <p className="muted">原创上 Trending {pct(row.trending_share)} · 市民回复 {n(row.citizen_replies)}、转发 {n(row.citizen_reposts)}</p>
   {Array.isArray(row.hourly_writes)&&row.hourly_writes.length>0&&<HourlyBars values={row.hourly_writes}/>}
  </section>
  <section>
   <h3>成员账号 · {n(row.members_total??members.length)}</h3>
   <ul>{shown.map(member=><li key={member.account_id} className="cluster-member">
    <Link to={`/accounts?q=${member.account_id}`}>@{member.username||member.account_id}</Link>
    <span className="muted">#{member.account_id} · {n(member.writes)} 条</span>
    <IdentityTags isBot={member.is_bot} botReasons={member.bot_reasons}/>
    {member.is_bot===true&&(member.bot_reasons||[]).length>0&&<span className="muted cluster-member-reasons">{(member.bot_reasons as string[]).map(reason=>botReasonLabels[reason]||reason).join('、')}</span>}
   </li>)}</ul>
   {members.length>MEMBERS_SHOWN&&<Button type="link" size="small" className="cluster-more" onClick={()=>setAll(value=>!value)}>{all?'收起':`显示全部 ${members.length} 个`}</Button>}
  </section>
  <section>
   <h3>代表帖子</h3>
   {examples.length?<ul>{examples.map(post=><li key={post.post_id}>
    <Link to={`/posts?post=${post.post_id}`}>#{post.post_id}</Link> <Tag>{exampleLabels[post.why]||post.why}</Tag>
    <span className="cluster-excerpt">{post.excerpt||'无文本内容'}</span>
   </li>)}</ul>:<p className="muted">没有代表帖子。</p>}
  </section>
  {!row.is_own&&<section>
   <h3>机器人判定原因 · {n(row.bot_flagged)} 个账号</h3>
   {reasons.length?<ul>{reasons.map(([reason,count])=><li key={reason}>{botReasonLabels[reason]||reason}<span className="muted"> · {n(count)} 人</span></li>)}</ul>:<p className="muted">没有成员被判成机器人。</p>}
   {(row.blue_cluster_ids||[]).length>0&&<p><Link to={blueLink(row)}>在 Blue 候选里查看这一簇的 {n(row.blue_candidates)} 个账号</Link></p>}
  </section>}
 </div>;
}

function ClusterTable({data}:{data:Row}) {
 const [search,setSearch]=useSearchParams();
 const sort:SortKey=isSortKey(search.get('sort'))?search.get('sort') as SortKey:'points',ascending=search.get('order')==='asc';
 const hours=num(data.window?.hours)||48,rows=orderClusters(data.own,data.clusters||[],sort,ascending);
 const blueCluster=search.get('blue_cluster'),fromBlue=clusterForBlue(rows,blueCluster);
 const opened=fromBlue?String(fromBlue.cluster_id):search.get('cluster');
 // Arriving from a Blue card: bring the opened cluster into view (rows are keyed by cluster id).
 useEffect(()=>{
  if(!fromBlue)return;
  const id=String(fromBlue.cluster_id),row=Array.from(document.querySelectorAll('tr[data-row-key]')).find(node=>node.getAttribute('data-row-key')===id);
  if(row&&typeof row.scrollIntoView==='function')row.scrollIntoView({block:'start'});
 },[fromBlue?.cluster_id]);
 const update=(changes:Record<string,string|null>)=>setSearch(previous=>{const next=new URLSearchParams(previous);for(const [key,value] of Object.entries(changes))if(value)next.set(key,value);else next.delete(key);return next;},{replace:true});
 const sortable=(key:SortKey)=>({key,sorter:true,sortDirections:['descend','ascend'] as SortOrder[],sortOrder:(sort===key?ascending?'ascend':'descend':null) as SortOrder});
 return <div className="stack">
  <div className="toolbar">
   <span className="muted">最近 {n(hours)} 小时（{time(data.window?.from)} – {time(data.window?.to)}）· 计算于 {time(data.generated_at)}</span>
   {data.stale&&<Tag color="processing">更新中</Tag>}
   <span className="muted">显示 {n((data.clusters||[]).length)} / {n(data.clusters_total)} 个簇 · 簇内 {n(data.clustered_accounts)} 个账号 · 校准市民 {n(data.citizen_accounts)} 个</span>
  </div>
  {blueCluster&&!fromBlue&&<Alert type="warning" showIcon title={`Blue 的簇 ${blueCluster} 不在当前的账号簇里`} description="簇每次重算都会变化；这个 Blue 候选的账号也可能没有被聚进任何账号簇。" action={<Link to="/blue">回到 Blue 候选</Link>}/>}
  <Table<Row> rowKey={row=>String(row.cluster_id)} dataSource={rows} pagination={false} scroll={{x:980}} size="middle"
   rowClassName={row=>row.is_own?'cluster-own':''}
   onChange={(_pagination,_filters,sorter)=>{
    const result=(Array.isArray(sorter)?sorter[0]:sorter) as SorterResult<Row>;
    const key=isSortKey(result?.columnKey)?result.columnKey:null;
    if(!key||!result.order)update({sort:null,order:null});
    else update({sort:key==='points'?null:key,order:result.order==='ascend'?'asc':null});
   }}
   expandable={{expandedRowKeys:opened?[opened]:[],onExpand:(open,row)=>update({cluster:open?String(row.cluster_id):null,blue_cluster:null}),expandedRowRender:row=><ClusterDetail row={row} hours={hours}/>}}
   columns={[
    {title:'簇',key:'name',fixed:'left',width:180,render:(_,row)=><span className="cluster-name"><strong>{clusterName(row)}</strong>{row.is_own?<Tag className="cluster-own-tag">对照</Tag>:row.members?.[0]&&<span className="muted">@{row.members[0].username||row.members[0].account_id} 等</span>}</span>},
    {title:'账号数',dataIndex:'accounts',align:'right',render:value=>n(value),...sortable('accounts')},
    {title:<span title="（市民回复 ×15 + 市民转发 ×10）÷ 48 小时。点赞和浏览没法归到作者，所以是下限估计，不是官方分数">市民互动分 / 时</span>,dataIndex:'citizen_points_per_hour',align:'right',render:value=>n(value),...sortable('points')},
    {title:'写入 / 时',dataIndex:'writes',align:'right',render:value=>n(num(value)===null?null:Number(value)/hours),...sortable('writes')},
    {title:'原创占比',dataIndex:'post_share',align:'right',render:value=>pct(value)},
    {title:'回给市民',dataIndex:'replies_to_citizens_share',align:'right',render:value=>pct(value)},
    {title:'立场',key:'stance',render:(_,row)=>stance(row)},
    {title:'疑似机器人',dataIndex:'bot_share',align:'right',render:(value,row)=><>{pct(value)}<span className="muted"> · {n(row.bot_flagged)} 个</span></>,...sortable('bots')},
    {title:'Blue 候选',dataIndex:'blue_candidates',align:'right',render:(value,row)=>row.is_own?'—':value==null?<span className="muted" title="Blue 页还没算过候选">—</span>:Number(value)>0&&(row.blue_cluster_ids||[]).length?<Link to={blueLink(row)}>{n(value)}</Link>:n(value)}
   ]}/>
  {data.method&&<Advanced title="聚类方法">
   <ul className="cluster-method">
    {(data.method.link||[]).map((text:string)=><li key={text}>{text}</li>)}
    {data.method.min_accounts!=null&&<li>至少 {n(data.method.min_accounts)} 个账号才算一个簇。</li>}
    {data.method.citizen_points&&<li>市民互动分：{data.method.citizen_points}</li>}
    {(data.method.not_used||[]).length>0&&<li>没有用上的方法：{data.method.not_used.join('、')}</li>}
   </ul>
  </Advanced>}
 </div>;
}

export default function AccountClusters() {
 // The server keeps each result for 10 minutes and recomputes stale ones in the background.
 const query=useApi<Row>('/clusters',{},300000,true);
 return <div className="stack">
  <Alert type="info" showIcon title={query.data?.note||clusterNote} description="同一簇的账号发过相同的长文本，或者集中转发同一组账号。可以用来看竞争对手的规模和打法；举报前仍要逐条核对证据。"/>
  {query.isPending
   ?<><p className="muted">正在计算账号簇。服务刚启动时第一次计算大约需要 20 秒。</p><Skeleton active paragraph={{rows:8}}/></>
   :<QueryState query={query}>{data=><ClusterTable data={data}/>}</QueryState>}
 </div>;
}

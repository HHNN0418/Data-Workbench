import {useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Input, Segmented, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, QueryState, fmt, time} from '../common';
import IdentityCalibration from './IdentityCalibration';
import {labels,reasonText} from './ownAccounts';
import './accountIdentity.css';

// Read-only: the labels come from the rule the Agents use to choose whom to reply to (agent_targeting).
// They live in ownAccounts so the role pages show the same names without loading this page.
export {labels,reasonText};
const stances:Record<string,string>={yes:'Yes',no:'No',undecided:'未决定',unrelated:'无关'};
const order=['citizen','bot','non_citizen','ours','teammate','public_figure'];

export default function AccountIdentity() {
 const [search,setSearch]=useSearchParams(),label=search.get('label')||'',q=search.get('q')||'';
 const [typed,setTyped]=useState(q);
 // The server keeps one result for five minutes and recomputes it in the background.
 const query=useApi<Row>('/accounts/identity',{label,q,limit:300},300000,true);
 function update(next:Record<string,string>) {
  const params=new URLSearchParams(search);
  for(const [key,value] of Object.entries(next)){if(value)params.set(key,value);else params.delete(key);}
  setSearch(params);
 }
 return <div className="stack">
  <Alert type="info" showIcon title="只读：每个活跃账号是谁" description="标签和 Agent 选回复对象用的是同一套规则；Agent 只回复「市民 NPC」。"/>
  <IdentityCalibration/>
  <QueryState query={query}>{data=>{
   const summary=data.summary||{},rows:Row[]=data.rows||[];
   return <div className="stack">
    <div className="toolbar"><span className="muted">最近 {fmt(data.window?.hours)} 小时活跃 {fmt(data.active_accounts)} 个账号 · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Metrics items={[...order.map(key=>({label:labels[key].text,value:summary[key]})),{label:'回过我们的市民',value:data.citizens_answering_us}]}/>
    {data.own_flagged&&<Alert type="warning" showIcon title={`我方 Agent ${fmt(data.own_flagged.ours?.flagged)}/${fmt(data.own_flagged.ours?.total)} 个、队友账号 ${fmt(data.own_flagged.teammate?.flagged)}/${fmt(data.own_flagged.teammate?.total)} 个会被这套规则判为机器人或非市民`}
     description="别的队如果用类似的规则找机器人，这些账号更容易被举报。只用来看风险，不用来调整发帖去躲避识别（D47）。"/>}
    <div className="toolbar">
     <Segmented aria-label="按身份筛选" value={label||'all'} onChange={value=>update({label:value==='all'?'':String(value)})}
      options={[{label:'全部',value:'all'},...order.map(key=>({label:`${labels[key].text} · ${fmt(summary[key])}`,value:key}))]}/>
     <Input.Search aria-label="搜索账号" allowClear placeholder="用户名或账号 ID" value={typed} onChange={event=>setTyped(event.target.value)} onSearch={value=>update({q:value.trim()})} className="identity-search"/>
    </div>
    <Table<Row> size="small" rowKey="account_id" dataSource={rows} pagination={{pageSize:50,showSizeChanger:false}} scroll={{x:960}}
     footer={()=>`显示 ${fmt(rows.length)} 个，符合条件的共 ${fmt(data.matched)} 个`}
     columns={[
      {title:'账号',key:'account',render:(_:unknown,row:Row)=><div><Link to={`/posts?author_id=${row.account_id}`}>{row.username?`@${row.username}`:`账号 ${row.account_id}`}</Link><div className="muted">#{row.account_id}</div></div>},
      {title:'身份',dataIndex:'label',render:(value:string,row:Row)=><><Tag color={labels[value]?.color}>{labels[value]?.text||value}</Tag>{row.figure_mentioned&&<Tag>我方原创帖会 @</Tag>}{row.suspended_at&&<Tag color="red">已封禁</Tag>}</>},
      {title:'依据',dataIndex:'reasons',render:(reasons:string[],row:Row)=>row.rule_reasons?(row.rule_reasons.length?<div><div className="muted">按规则会被判为{labels[row.rule_label]?.text||'非市民'}：</div><ul className="identity-reasons">{row.rule_reasons.map((reason:string)=><li key={reason}>{reasonText[reason]||reason}</li>)}</ul></div>:<span className="muted">按规则会被当成市民</span>)
       :reasons?.length?<ul className="identity-reasons">{reasons.map(reason=><li key={reason}>{reasonText[reason]||reason}</li>)}</ul>:<span className="muted">{row.label==='citizen'?'没有触发任何机器人或非市民规则':'—'}</span>},
      {title:'24 小时',key:'activity',render:(_:unknown,row:Row)=><div>{fmt(row.posts_24h)} 帖 · {fmt(row.replies_24h)} 回复{row.reposts_24h?` · ${fmt(row.reposts_24h)} 转发`:''}<div className="muted">累计 {fmt(row.actions_total)}，48 小时 {fmt(row.writes_48h)}</div></div>},
      {title:'立场',dataIndex:'stance',render:(value:string)=>stances[value]||value},
      {title:'最近一条',key:'last',render:(_:unknown,row:Row)=>row.last_post_id?<div className="identity-last"><Link to={`/posts?post=${row.last_post_id}`}>{time(row.last_at)}</Link><div>{row.last_excerpt}</div></div>:'—'},
      {title:'和我们',key:'contact',render:(_:unknown,row:Row)=>row.label==='ours'||row.label==='teammate'?'—':<div>我方回复 {fmt(row.our_replies_24h)} 次{row.our_last_reply_at&&<div className="muted">最近 {time(row.our_last_reply_at)}</div>}{row.answered_us_24h>0&&<div><Tag color="green">回过我们 {fmt(row.answered_us_24h)} 次</Tag></div>}</div>},
     ]}/>
    <ul className="muted identity-caveats">{(data.caveats||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>
   </div>;
  }}</QueryState>
 </div>;
}

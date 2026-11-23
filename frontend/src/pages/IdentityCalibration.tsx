import {Alert, Collapse, Table, Tag} from 'antd';
import {Link} from 'react-router-dom';
import {useApi, type Row} from '../api';
import {Metrics, QueryState, fmt, time} from '../common';

// Read-only: the citizen rule scored against every official hourly NPC reply count (npc_calibration).
const flags:Record<string,{text:string,color?:string}>={clean:{text:'干净',color:'green'},irregular:{text:'报告不规则'},outage:{text:'停机',color:'orange'},low_collection:{text:'采集不足',color:'orange'}};
const signed=(value:unknown)=>typeof value==='number'?(value>0?`+${value}`:String(value)):'—';
const kinds:Record<string,string>={replies:'回复',reposts:'转发',likes:'点赞',follows:'关注'};
const pair=(official:unknown,collected:unknown)=>`${fmt(official)}/${fmt(collected)}`;

function scoreText(score:Row|undefined){
 if(!score?.hours)return '—';
 return `误差 ${fmt(score.mae)} · 相关 ${fmt(score.corr)} · 命中 ${fmt(score.exact_hours)}/${fmt(score.hours)} · 偏差 ${signed(score.bias)}`;
}

function Accounts({rows}:{rows:Row[]}){
 if(!rows.length)return <span className="muted">没有</span>;
 return <span className="identity-calibration-accounts">{rows.map(row=><Link key={row.account_id} to={`/processing/bots?q=${row.account_id}`}>{row.username?`@${row.username}`:`#${row.account_id}`}</Link>)}</span>;
}

export default function IdentityCalibration(){
 const query=useApi<Row>('/accounts/calibration',{},300000);
 return <section className="panel stack" aria-label="识别准确率">
  <div className="toolbar"><h3>识别准确率（对照官方每小时 NPC 回复、转发、点赞和关注数）</h3></div>
  <QueryState query={query}>{data=>{
   const all=data.metrics?.all||{},recent=data.metrics?.recent||{},labels=data.labels||{};
   const variants:Row[]=data.variants||[],hours:Row[]=[...(data.hours||[])].reverse().slice(0,24);
   const promotable=new Set<string>(data.promotable||[]);
   return <div className="stack">
    {data.alert&&<Alert type="warning" showIcon title="规则偏离官方数" description={data.alert}/>}
    <div className="toolbar"><span className="muted">最新官方小时 {time(data.latest_report)} · 干净小时 {fmt(data.hours_clean)}/{fmt(data.hours_total)} · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Metrics items={[
     {label:'全部干净小时：每小时误差',value:all.mae,hint:`相关 ${fmt(all.corr)} · 命中 ${fmt(all.exact_hours)}/${fmt(all.hours)} · 偏差 ${signed(all.bias)}`},
     {label:'最近 24 个干净小时：每小时误差',value:recent.mae,hint:`相关 ${fmt(recent.corr)} · 命中 ${fmt(recent.exact_hours)}/${fmt(recent.hours)} · 偏差 ${signed(recent.bias)}`},
     {label:'确定是 NPC 的账号',value:labels.npc,hint:'由回复、转发或关注证明；选回复对象时优先'},
     {label:'确定不是 NPC 的账号',value:labels.non_npc,hint:'官方某一类为 0 的小时里对我们做过这件事'},
     {label:'被规则误算为市民',value:(labels.misjudged_citizens||[]).length,hint:'应该为 0'},
    ]}/>
    <Table<Row> size="small" rowKey="key" dataSource={variants} pagination={false} scroll={{x:760}} caption="规则对比：每个理由去掉后、每个候选理由加上后的结果；只有两个窗口都更好、赛前 NPC 通过率不降时才标为可上线"
     columns={[
      {title:'规则',key:'label',render:(_:unknown,row:Row)=><>{row.label}{row.key==='live'&&<Tag color="blue">线上</Tag>}{promotable.has(row.key)&&<Tag color="green">可上线</Tag>}{row.candidate&&!promotable.has(row.key)&&<Tag>候选</Tag>}</>},
      {title:'全部干净小时',key:'all',render:(_:unknown,row:Row)=>scoreText(row.all)},
      {title:'最近 24 小时',key:'recent',render:(_:unknown,row:Row)=>scoreText(row.recent)},
      {title:'赛前 NPC 通过率',dataIndex:'precompetition_pass',render:(value:number|null)=>value===null||value===undefined?'—':`${(value*100).toFixed(1)}%`},
     ]}/>
    <Collapse size="small" items={[
     {key:'hours',label:'最近 24 个官方小时',children:<Table<Row> size="small" rowKey="end" dataSource={hours} pagination={false} scroll={{x:640}}
      columns={[
       {title:'官方小时（结束）',dataIndex:'end',render:(value:string)=>time(value)},
       {title:'状态',dataIndex:'flag',render:(value:string)=><Tag color={flags[value]?.color}>{flags[value]?.text||value}</Tag>},
       {title:'官方 NPC 回复',dataIndex:'official_replies'},
       {title:'规则算出',dataIndex:'predicted_npc_replies',render:(value:number,row:Row)=><span className={value===row.official_replies?'':'identity-calibration-miss'}>{fmt(value)}</span>},
       {title:'回复我们的总数',dataIndex:'replies_to_us'},
       {title:'官方浏览',dataIndex:'official_views'},
       {title:'转发 · 点赞 · 关注（官方/采集）',key:'others',render:(_:unknown,row:Row)=>`${pair(row.official_reposts,row.reposts_to_us)} · ${pair(row.official_likes,row.likes_to_us)} · ${pair(row.official_follows,row.follows_of_us)}`},
      ]}/>},
     {key:'npc',label:`确定是 NPC 的账号 · ${fmt(labels.npc)}`,children:<div className="stack">
      <div><strong>最近确认的：</strong><Accounts rows={labels.newest_npc||[]}/></div>
      <div className="muted">{Object.entries(kinds).map(([key,text])=>`${text}：是 ${fmt(labels.by_kind?.[key]?.npc)} · 不是 ${fmt(labels.by_kind?.[key]?.non_npc)}`).join('；')}</div>
     </div>},
     {key:'labels',label:`确定不是 NPC 的账号 · ${fmt(labels.non_npc)}`,children:<div className="stack">
      <div><strong>被规则误算为市民：</strong><Accounts rows={labels.misjudged_citizens||[]}/></div>
      <div><strong>最近确认的：</strong><Accounts rows={labels.newest_non_npc||[]}/></div>
      {!!(labels.contradictions||[]).length&&<Alert type="info" showIcon title={`${labels.contradictions.length} 个小时对不上`} description="官方计数和采集到的人对不上：可能有本队账号没登记，或者采集漏了。"/>}
     </div>},
     {key:'method',label:'方法',children:<ul className="muted identity-caveats">{(data.method||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>},
    ]}/>
   </div>;
  }}</QueryState>
 </section>;
}

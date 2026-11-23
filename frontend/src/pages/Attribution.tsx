import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Segmented, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, Panel, QueryState, time} from '../common';
import {n, pct} from './pipelineModel';
import {actionLabels, concernLabel, stanceLabels, sydney} from './stageViews';
import './stageViews.css';

// Per-post outcomes from post_attribution (GET /api/results/attribution): which concerns, actions, roles and hours
// drew citizen reactions. The reward is a proxy; the page says so.
const groupings=[{value:'action',label:'动作'},{value:'arms',label:'顾虑 × 动作'},{value:'concern',label:'顾虑'},{value:'agent',label:'角色'},
 {value:'hour',label:'发布时段'},{value:'target_stance',label:'目标立场'},{value:'depth',label:'线程层数'},{value:'pipeline',label:'流程'}] as const;
type Grouping=typeof groupings[number]['value'];
const pipelineLabels:Record<string,string>={staged:'分步流程',single:'单步'};

export function groupLabel(grouping:string,row:Row):string {
 const key=row.key;
 if(key==='none'||key===null||key===undefined)return '未记录';
 switch(grouping){
  case 'action':return actionLabels[key]||key;
  case 'concern':return concernLabel(key);
  case 'agent':return row.label||key;
  case 'hour':return `悉尼 ${String(key).padStart(2,'0')}:00`;
  case 'target_stance':return stanceLabels[key]||key;
  case 'depth':return `第 ${key} 层`;
  case 'pipeline':return pipelineLabels[key]||key;
  default:return String(key);
 }
}

const rate=(value:unknown)=>pct(value);
const groupColumns=(grouping:string)=>[
 {title:'分组',key:'group',render:(_:unknown,row:Row)=>grouping==='agent'&&row.key!=='none'?<Link to={`/ai/agents/${row.key}`}>{groupLabel(grouping,row)}</Link>:groupLabel(grouping,row)},
 {title:'已结算 / 全部',key:'count',render:(_:unknown,row:Row)=>`${n(row.settled)} / ${n(row.posts)}`},
 {title:'平均回报',dataIndex:'mean_reward',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'成功率',dataIndex:'success_rate',align:'right' as const,render:rate},
 {title:'市民回复 / 条',dataIndex:'npc_replies_per_post',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'点赞 / 条',dataIndex:'likes_per_post',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'转发 / 条',dataIndex:'reposts_per_post',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'偏 Yes 的回复',dataIndex:'reply_yes',align:'right' as const,render:(value:unknown)=>n(value)},
];
const armColumns=(minimum:unknown)=>[
 {title:'顾虑',dataIndex:'concern',render:(value:unknown)=>concernLabel(value)},
 {title:'动作',dataIndex:'action',render:(value:string)=>actionLabels[value]||value},
 {title:'已结算',dataIndex:'posts',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'成功',dataIndex:'successes',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'成功率',dataIndex:'success_rate',align:'right' as const,render:rate},
 {title:'平均回报',dataIndex:'mean_reward',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'市民回复',dataIndex:'npc_replies',align:'right' as const,render:(value:unknown)=>n(value)},
 {title:'雷达排序',dataIndex:'ranked',render:(value:boolean)=>value?<Tag color="green">参与排序</Tag>:<span className="muted">不足 {n(minimum)} 条</span>},
];

export default function Attribution() {
 const [search,setSearch]=useSearchParams();
 const grouping=(groupings.some(item=>item.value===search.get('group'))?search.get('group'):'action') as Grouping;
 const query=useApi<Row>('/results/attribution',{},300000);
 function choose(value:string){const params=new URLSearchParams(search);if(value==='action')params.delete('group');else params.set('group',value);setSearch(params);}
 return <div className="stack">
  <QueryState query={query}>{data=>{
   const totals=data.totals||{},latest:Row[]=data.latest||[];
   const rows:Row[]=grouping==='arms'?data.arms||[]:data.groups?.[grouping]||[];
   return <div className="stack">
    <Alert type="info" showIcon title="每条发布的效果（代理指标）" description={<ul className="view-notes">{(data.notes||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>}/>
    <div className="toolbar"><span className="muted">最近 {n(data.window?.hours)} 小时发布 · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Metrics items={[{label:'已记录',value:totals.posts},{label:'已结算',value:totals.settled},
     {label:'等待结算',value:totals.pending,hint:totals.next_settle_at?`下一条 ${time(totals.next_settle_at)} 结算`:undefined},
     {label:'平均回报',value:totals.mean_reward},{label:'成功率',value:pct(totals.success_rate),hint:`回报 ≥ ${n(data.success_reward)}`},
     {label:'市民回复 / 条',value:totals.npc_replies_per_post},{label:'偏 Yes 的回复',value:totals.reply_yes}]}/>
    <Panel title="分组平均" extra={<Link to="/ai/analysis">顾虑雷达</Link>}>
     <div className="stack">
      <Segmented aria-label="分组方式" value={grouping} onChange={value=>choose(String(value))} options={groupings.map(item=>({label:item.label,value:item.value}))}/>
      {grouping==='arms'
       ?<Table<Row> size="small" rowKey={row=>`${row.concern}:${row.action}`} dataSource={rows} pagination={rows.length>20?{pageSize:20,showSizeChanger:false}:false} scroll={{x:900}} columns={armColumns(data.min_arm_posts)}/>
       :<Table<Row> size="small" rowKey={row=>String(row.key)} dataSource={rows} pagination={rows.length>24?{pageSize:24,showSizeChanger:false}:false} scroll={{x:900}} columns={groupColumns(grouping)}/>}
     </div>
    </Panel>
    <Panel title={`最近已结算的 ${n(latest.length)} 条`} extra={<Link to="/ai/facts">事实库</Link>}>
     <Table<Row> size="small" rowKey="post_id" dataSource={latest} pagination={{pageSize:20,showSizeChanger:false}} scroll={{x:1100}}
      columns={[
       {title:'发布（悉尼）',dataIndex:'published_at',render:(value:string,row:Row)=><Link to={`/posts?post=${row.post_id}`}>{sydney(value)}</Link>},
       {title:'角色',dataIndex:'agent_name'},
       {title:'动作',dataIndex:'action',render:(value:string)=>actionLabels[value]||value},
       {title:'顾虑',dataIndex:'concern',render:(value:unknown)=>concernLabel(value)},
       {title:'目标',key:'target',render:(_:unknown,row:Row)=>row.action==='reply'?<span>{stanceLabels[row.target_stance]||'—'}<div className="view-cell-sub">第 {n(row.thread_depth)} 层</div></span>:'—'},
       {title:'字数',dataIndex:'body_chars',align:'right',render:(value:unknown)=>n(value)},
       {title:'市民回复',dataIndex:'npc_replies',align:'right',render:(value:unknown)=>n(value)},
       {title:'回复立场',key:'replies',render:(_:unknown,row:Row)=>`Yes ${n(row.reply_yes)} · 犹豫 ${n(row.reply_undecided)} · No ${n(row.reply_no)}`},
       {title:'点赞',dataIndex:'likes',align:'right',render:(value:unknown)=>n(value)},
       {title:'转发',dataIndex:'reposts',align:'right',render:(value:unknown)=>n(value)},
       {title:'回报',dataIndex:'reward',align:'right',render:(value:unknown)=>n(value)},
      ]}/>
    </Panel>
   </div>;
  }}</QueryState>
 </div>;
}

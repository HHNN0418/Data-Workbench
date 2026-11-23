import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Segmented, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, Panel, QueryState, time} from '../common';
import {n, num, pct} from './pipelineModel';
import {concernLabel, stanceLabels} from './stageViews';
import './stageViews.css';

// Annotation progress and what it found (GET /api/processing/annotations); stance, concerns and thread depth.
const scopes=[{value:'citizen',label:'只看市民'},{value:'all',label:'全部帖子'}];
const depthLabels:Record<string,string>={'0':'原帖','1':'第 1 层','2':'第 2 层','3':'第 3 层','4–6':'第 4–6 层','7+':'第 7 层及更深',unknown:'未知'};

export function Bars({rows,label}:{rows:Row[];label:(key:string)=>string}) {
 return <ul className="view-bars">{rows.map(row=><li key={row.key} className="view-bar">
  <span>{label(row.key)}</span>
  <span className="view-bar-track" aria-hidden="true"><span className="view-bar-fill" style={{width:`${Math.min(100,(num(row.share)||0)*100)}%`}}/></span>
  <span className="view-bar-value">{n(row.count)} · {pct(row.share)}</span>
 </li>)}</ul>;
}

export default function Processing() {
 const [search,setSearch]=useSearchParams(),scope=search.get('scope')==='all'?'all':'citizen';
 const query=useApi<Row>('/processing/annotations',{},120000);
 function choose(value:string){const params=new URLSearchParams(search);if(value==='citizen')params.delete('scope');else params.set('scope',value);setSearch(params);}
 return <div className="stack">
  <Alert type="info" showIcon title="每条帖子采集进来后，会标注立场、顾虑和线程深度" description="标注每分钟跑一次，Agent 选读和选目标都读这些标注。这里显示进度、积压和最近 24 小时的分布。"/>
  <QueryState query={query}>{data=>{
   const progress=data.progress||{},totals=data.window_totals||{},concerns:Row[]=data.concerns||[];
   return <div className="stack">
    <div className="toolbar"><span className="muted">计算于 {time(data.generated_at)} · 规则版本 {progress.classifier}</span>{data.stale&&<Tag color="processing">更新中</Tag>}{progress.backlog>0&&<Tag color="warning">积压 {n(progress.backlog)} 条</Tag>}</div>
    <Metrics items={[{label:'已采集帖子',value:progress.posts_total},{label:'已按当前规则标注',value:progress.current_classifier,hint:`覆盖 ${pct(progress.coverage)}`},
     {label:'积压',value:progress.backlog,hint:'未标注或旧规则'},{label:'最近一次标注',value:time(progress.last_annotated_at),
      hint:progress.last_pass?`上一轮处理 ${n(progress.last_pass.posts)} 条`:undefined},
     {label:'机器人账号',value:progress.bot_accounts},{label:`${n(data.window?.hours)} 小时内的帖子`,value:totals.posts,
      hint:`市民 ${n(totals.citizen)} · 我方 ${n(totals.own)} · 机器人 ${n(totals.bot)}`}]}/>
    <div className="toolbar">
     <Segmented aria-label="统计范围" value={scope} onChange={value=>choose(String(value))} options={scopes}/>
     <span className="muted">「只看市民」去掉了我方账号和被标为机器人的账号</span>
    </div>
    <div className="two-col">
     <Panel title="立场分布" extra={<Link to="/analysis">舆论与洞察</Link>}><Bars rows={data.stance?.[scope]||[]} label={key=>stanceLabels[key]||key}/></Panel>
     <Panel title="线程深度" extra={<span className="muted">第 {n(data.max_reply_depth)} 层起不回复</span>}><Bars rows={data.depth?.[scope]||[]} label={key=>depthLabels[key]||key}/></Panel>
    </div>
    <Panel title="顾虑分布" extra={<span className="muted">有顾虑的帖子 {n(totals.with_concern?.[scope])} 条；一条帖子可以有几个顾虑</span>}>
     <Table<Row> size="small" rowKey="concern" dataSource={concerns} pagination={concerns.length>20?{pageSize:20,showSizeChanger:false}:false} scroll={{x:720}}
      columns={[
       {title:'顾虑',dataIndex:'concern',render:(value:unknown)=>concernLabel(value)},
       {title:'全部',dataIndex:'all',align:'right',render:(value:unknown)=>n(value)},
       {title:'市民',dataIndex:'citizen',align:'right',render:(value:unknown)=>n(value)},
       {title:'市民 · 犹豫',dataIndex:'citizen_undecided',align:'right',render:(value:unknown)=>n(value)},
       {title:'市民 · 偏 No',dataIndex:'citizen_no',align:'right',render:(value:unknown)=>n(value)},
       {title:'市民 · 偏 Yes',dataIndex:'citizen_yes',align:'right',render:(value:unknown)=>n(value)},
      ]}/>
    </Panel>
    <div className="view-links"><Link to="/posts">帖子流</Link><Link to="/processing/bots">账号识别</Link><Link to="/ai/facts?tab=extraction">事实提取</Link></div>
    <ul className="view-notes">{(data.notes||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>
   </div>;
  }}</QueryState>
 </div>;
}

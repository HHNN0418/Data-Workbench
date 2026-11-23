import {Link} from 'react-router-dom';
import {Alert, Descriptions, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, Panel, QueryState, fmt, time} from '../common';
import {StatusTag} from '../ui/status';
import {n} from './pipelineModel';
import {seconds} from './stageViews';
import './stageViews.css';

// Reload and maintenance history (GET /api/maintenance/history). Read-only: nothing on this page starts a reload,
// ends maintenance or deletes data; backups are created on the collection page.
export const resultLabels:Record<string,{text:string;tone:'ok'|'warn'|'danger'|'neutral'}>={ready:{text:'完成',tone:'ok'},
 aborted_existing_backend_kept:{text:'中止，保留原后端',tone:'warn'},failed:{text:'失败',tone:'danger'}};
const phaseLabels:Record<string,string>={draining:'等待已有任务结束',completed:'已完成',aborted:'已中止'};
const eventLabels:Record<string,string>={maintenance_started:'开始维护',maintenance_completed:'维护完成',maintenance_aborted:'维护中止'};
const bytes=(value:unknown)=>{const v=Number(value);return Number.isFinite(v)?v>=1e9?`${(v/1e9).toFixed(1)} GB`:v>=1e6?`${(v/1e6).toFixed(1)} MB`:`${n(v/1e3)} KB`:'—';};

export default function Maintenance() {
 const query=useApi<Row>('/maintenance/history',{},30000);
 return <div className="stack">
  <Alert type="info" showIcon title="只读：维护状态与重载记录" description="这一页只显示记录，不会触发重载、结束维护或删除数据。数据库备份在「采集运行」页创建。"/>
  <QueryState query={query}>{data=>{
   const state=data.state||{},history:Row[]=data.history||[],summary=data.history_summary||{},retention=data.retention||{};
   const counts=Object.entries(retention.last_result?.counts||{});
   return <div className="stack">
    <div className="toolbar"><span className="muted">读取于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Panel title="Agent 维护状态">
     <Descriptions size="small" column={{xs:1,sm:2,lg:4}} items={[
      {key:'state',label:'现在',children:state.active?<StatusTag tone="warn">维护中</StatusTag>:<StatusTag tone="ok">正常运行</StatusTag>},
      {key:'phase',label:'最近一次',children:phaseLabels[state.phase]||fmt(state.phase)},
      {key:'reason',label:'原因',children:fmt(state.reason)},
      {key:'time',label:'时间',children:`${time(state.started_at)} → ${state.finished_at?time(state.finished_at):'进行中'}`}]}/>
    </Panel>
    <Metrics items={[{label:'重载记录',value:summary.reloads},{label:'完成',value:summary.results?.ready??0},
     {label:'中止或失败',value:(summary.reloads||0)-(summary.results?.ready??0)},{label:'最近一次完成',value:time(summary.last_ready_at)}]}/>
    <Panel title="重载记录" extra={<span className="muted">reports/generated/unified-*-deployment.json</span>}>
     <Table<Row> size="small" rowKey="name" dataSource={history} pagination={history.length>20?{pageSize:20,showSizeChanger:false}:false} scroll={{x:1000}}
      columns={[
       {title:'开始',dataIndex:'started_at',render:(value:unknown,row:Row)=><div>{time(value)}<div className="view-cell-sub">{row.name}</div></div>},
       {title:'目的',dataIndex:'purpose',render:(value:unknown,row:Row)=>row.unreadable?<span className="muted">记录文件无法读取</span>:fmt(value)},
       {title:'结果',dataIndex:'result',render:(value:string,row:Row)=>row.unreadable?'—':<><StatusTag tone={resultLabels[value]?.tone||'neutral'}>{resultLabels[value]?.text||value||'未知'}</StatusTag>{row.failed_stage&&<div className="view-cell-sub">停在 {row.failed_stage}</div>}</>},
       {title:'等待已有任务',dataIndex:'drain_seconds',align:'right',render:(value:unknown)=>seconds(value)},
       {title:'总用时',dataIndex:'total_seconds',align:'right',render:(value:unknown)=>seconds(value)},
       {title:'结束',dataIndex:'finished_at',render:(value:unknown)=>time(value)},
      ]}/>
    </Panel>
    <div className="two-col">
     <Panel title="最近的维护事件">
      <Table<Row> size="small" rowKey={row=>`${row.at}:${row.kind}`} dataSource={data.events||[]} pagination={false}
       columns={[{title:'时间',dataIndex:'at',render:(value:unknown)=>time(value)},
        {title:'事件',dataIndex:'kind',render:(value:string)=>eventLabels[value]||value},{title:'说明',dataIndex:'message'}]}/>
     </Panel>
     <Panel title="数据保留与备份" extra={<Link to="/operations">采集运行（创建备份）</Link>}>
      <div className="stack">
       <p className="muted">只自动清理过期的诊断记录（接口日志、无引用的原始记录、过期租约和已完成的队列任务），保留 {n(retention.retention_days)} 天，每 {n((retention.interval_seconds||0)/60)} 分钟一轮。业务数据不会自动删除。</p>
       {counts.length>0&&<p className="muted">上一轮清理：{counts.map(([table,count])=>`${table} ${n(count)}`).join(' · ')}</p>}
       {(data.backups||[]).length?<ul className="view-rules">{(data.backups as Row[]).map(file=><li key={file.name}>
        <a href={file.url}>{file.name}</a><span>{time(file.modified_at)} · {bytes(file.bytes)}</span></li>)}</ul>
        :<p className="muted">还没有数据库备份。</p>}
      </div>
     </Panel>
    </div>
   </div>;
  }}</QueryState>
 </div>;
}

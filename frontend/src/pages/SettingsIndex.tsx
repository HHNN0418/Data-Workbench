import type {ReactNode} from 'react';
import {Link} from 'react-router-dom';
import {Alert, Descriptions, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Panel, QueryState, fmt, time} from '../common';
import {StatusTag} from '../ui/status';
import {n} from './pipelineModel';
import {actionLabels} from './stageViews';
import './stageViews.css';

// Index of the team settings and roles (GET /api/settings/summary); each block links to the one place it is edited.
const readingModes:Record<string,string>={rules:'规则选读（不调用模型）',model:'模型选读'};
const reviewModes:Record<string,string>={auto:'自动生效',manual:'人工审核'};
const pipelines:Record<string,string>={staged:'分步流程（决策 → 写作 → 检查）',single:'单步'};
const yes=(value:unknown,on='开',off='关')=>value===true?<StatusTag tone="ok">{on}</StatusTag>:value===false?<StatusTag tone="neutral">{off}</StatusTag>:'—';
const health=(value:unknown,ok='正常',bad='异常')=>value===true?<StatusTag tone="ok">{ok}</StatusTag>:value===false?<StatusTag tone="danger">{bad}</StatusTag>:'—';
const provider=(row:Row|undefined)=>row?.provider_name?`${row.provider_name}${row.provider_model?` · ${row.provider_model}`:''}`:row?.provider_id?`连接 ${String(row.provider_id).slice(0,8)}`:'默认连接';

function Block({title,to,label,items}:{title:string;to:string;label:string;items:Array<{label:string;children:ReactNode}>}) {
 return <Panel title={title} extra={<Link to={to}>在「{label}」修改</Link>}>
  <Descriptions size="small" column={1} items={items.map((item,index)=>({key:index,...item}))}/>
 </Panel>;
}

export default function SettingsIndex() {
 const query=useApi<Row>('/settings/summary',{},30000);
 return <div className="stack">
  <Alert type="info" showIcon title="只读：全部设置的索引" description="每一块写明现在的状态和唯一的修改位置；这一页不改任何设置。"/>
  <QueryState query={query}>{data=>{
   const team=data.team||{},status=data.status||{},summary=data.summary||{},roles:Row[]=data.roles||[];
   const missing=roles.filter(role=>role.provider_missing);
   return <div className="stack">
    <div className="toolbar"><span className="muted">读取于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    {missing.length>0&&<Alert type="warning" showIcon title={`${missing.length} 个角色选的 AI 连接已不存在`} description={`${missing.map(role=>role.name).join('、')}：自动轮次会因为没有 AI 连接而失败。到「角色」里给它重新选一个连接。`}/>}
    <div className="two-col">
     <Block title="队伍设置" to="/ai/agents?tab=settings" label="队伍设置" items={[
      {label:'持续运行',children:yes(team.enabled,'运行中','已停止')},{label:'我方立场',children:fmt(team.team_side)},
      {label:'角色上限',children:`${n(summary.roles)} / ${n(team.max_accounts)}，启用 ${n(summary.enabled)}`},
      {label:'两次动作的最短间隔',children:`${n(team.global_interval_seconds)} 秒`},
      {label:'不和别队 Agent 互动',children:yes(team.block_agent_interactions)},
      {label:'维护',children:status.agents?.maintenance?.active?<StatusTag tone="warn">维护中</StatusTag>:<StatusTag tone="ok">无</StatusTag>}]}/>
     <Block title="AI 连接" to="/ai/settings" label="AI 连接" items={[
      {label:'已保存的连接',children:n(data.providers?.count)},{label:'默认连接',children:provider(data.providers?.default)},
      {label:'默认连接可用',children:health(status.ai?.default_ok,'可用','不可用')}]}/>
     <Block title="发布审核" to="/ai/review" label="发布审核" items={[
      {label:'发布前 AI 审核',children:yes(data.review?.enabled)},{label:'审核用的连接',children:provider(data.review)},
      {label:'只允许英文',children:yes(data.review?.english_only,'是','否')},
      {label:'审核连接',children:health(status.review?.connection_ok,'可用','不可用')}]}/>
     <Block title="事实库与写作流程" to="/ai/facts" label="事实库" items={[
      {label:'新事实',children:reviewModes[data.fact_bank?.review_mode]||fmt(data.fact_bank?.review_mode)},
      {label:'从新闻自动提取',children:yes(data.fact_bank?.extract_enabled)},
      {label:'写作流程',children:pipelines[data.fact_bank?.pipeline]||fmt(data.fact_bank?.pipeline)},
      {label:'提取用的连接',children:provider(data.fact_bank)}]}/>
     <Block title="资料筛选" to="/ai/reading" label="资料筛选" items={[
      {label:'选读方式',children:Object.entries(summary.reading_modes||{}).map(([mode,count])=>`${readingModes[mode]||mode} ${n(count)} 个角色`).join('；')||'—'}]}/>
     <Block title="采集与积分同步" to="/operations" label="采集运行" items={[
      {label:'自动采集',children:yes(status.collection?.scheduler_enabled)},{label:'采集状态',children:health(status.collection?.healthy)},
      {label:'积分同步（浏览器扩展）',children:health(status.score_sync?.healthy)},
      {label:'最近同步',children:time(status.score_sync?.last_collected_at)}]}/>
    </div>
    <Panel title={`角色 · ${n(roles.length)}`} extra={<Link to="/ai/agents">在「角色」修改</Link>}>
     <Table<Row> size="small" rowKey="id" dataSource={roles} pagination={false} scroll={{x:1000}}
      columns={[
       {title:'角色',dataIndex:'name',render:(value:string,row:Row)=><div><Link to={`/ai/agents/${row.id}`}>{value||row.id}</Link><div className="view-cell-sub">{row.username?`@${row.username}`:''}{row.account_id?` #${row.account_id}`:''}</div></div>},
       {title:'状态',dataIndex:'enabled',render:(value:boolean,row:Row)=><>{value?<StatusTag tone="ok">启用</StatusTag>:<StatusTag tone="neutral">停用</StatusTag>}{row.has_error&&<StatusTag tone="warn">有报错</StatusTag>}</>},
       {title:'班次组',dataIndex:'group',render:(value:string)=>value||<span className="muted">不排班</span>},
       {title:'选读',dataIndex:'reading_selection_mode',render:(value:string)=>readingModes[value]||value},
       {title:'AI 连接',key:'provider',render:(_:unknown,row:Row)=>row.provider_missing?<StatusTag tone="danger">连接已不存在</StatusTag>:provider(row)},
       {title:'可选动作',dataIndex:'allowed_actions',render:(value:string[])=>(value||[]).map(action=>actionLabels[action]||action).join('、')||'—'},
       {title:'每日上限',dataIndex:'daily_limit',align:'right',render:(value:unknown)=>n(value)},
       {title:'间隔',dataIndex:'interval_seconds',align:'right',render:(value:unknown)=>value?`${n(Number(value)/60)} 分钟`:'—'},
      ]}/>
    </Panel>
   </div>;
  }}</QueryState>
 </div>;
}

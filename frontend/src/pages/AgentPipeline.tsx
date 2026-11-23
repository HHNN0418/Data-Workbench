import {Link} from 'react-router-dom';
import {Alert, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, Panel, QueryState, time} from '../common';
import {n, pct} from './pipelineModel';
import {actionLabels, clock, duration, seconds} from './stageViews';
import './stageViews.css';

// The staged pipeline step by step over six hours (GET /api/pipeline/steps): reading → decide → write → check → publish.
export const errorLabels:Record<string,string>={output_truncated:'输出被截断',reasoning_only:'只返回推理、没有正文',provider_error:'模型服务出错',timeout:'超时',rate_limited:'被限流',error:'其他错误'};
export const phaseLabels:Record<string,string>={reading:'选读',model:'模型请求',headers:'等待模型响应',thinking:'模型思考中',receiving:'接收回答',complete:'回答之后（检查或保存）',unknown:'未记录'};

function stepDetail(step:Row) {
 if(step.key==='reading')return <span>规则选读 {n(step.rules_runs)} · 模型选读 {n(step.model_runs)}</span>;
 if(step.key==='publish'){
  const by=step.by_action||{};
  return <span>{Object.entries(step.actions||{}).map(([action,count])=>`${actionLabels[action]||action} ${n(count)}`).join(' · ')||'—'}
   {by.post&&<div className="view-cell-sub">原创中位 {duration(by.post.median_ms)}</div>}
   {by.reply&&<div className="view-cell-sub">回复中位 {duration(by.reply.median_ms)}（写好到发出）</div>}</span>;
 }
 const errors:Row[]=step.top_errors||[];
 return errors.length?<span>{errors.map(item=>`${errorLabels[item.reason]||item.reason} ${n(item.count)}`).join(' · ')}</span>:<span className="muted">没有失败</span>;
}

export default function AgentPipeline() {
 const query=useApi<Row>('/pipeline/steps',{},60000);
 return <div className="stack">
  <QueryState query={query}>{data=>{
   const totals=data.totals||{},queue=data.queue||{},steps:Row[]=data.steps||[],publish=steps.find(step=>step.key==='publish')||{};
   const actions=publish.actions||{};
   return <div className="stack">
    {queue.maintenance?.active&&<Alert type="warning" showIcon title="Agent 正在维护" description={queue.maintenance.reason||'维护结束后自动恢复。'}/>}
    <div className="toolbar"><span className="muted">最近 {n(data.window?.hours)} 小时 · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Metrics items={[{label:'完成的轮次',value:totals.completed,hint:`一轮中位 ${seconds(totals.round_seconds?.median)}，p90 ${seconds(totals.round_seconds?.p90)}`},
     {label:'失败的轮次',value:totals.failed},{label:'失败率',value:pct(totals.failure_rate)},
     {label:'发布',value:totals.published,hint:`原创 ${n(actions.post)} · 回复 ${n(actions.reply)}`},
     {label:'待发布队列',value:queue.pending_actions,hint:queue.uncertain_actions?`其中 ${n(queue.uncertain_actions)} 条结果未确认`:undefined},
     {label:'未确认通知',value:queue.inbox_unresolved}]}/>
    <Panel title="每一步" extra={<div className="view-links"><Link to="/ai/agents">角色</Link><Link to="/ai/agents?tab=inbox">待处理</Link><Link to="/ai/review">发布审核</Link></div>}>
     <Table<Row> size="small" rowKey="key" dataSource={steps} pagination={false} scroll={{x:900}}
      columns={[
       {title:'步骤',dataIndex:'label',render:(value:string,_row:Row,index:number)=><strong>{index+1}. {value}</strong>},
       {title:'次数',dataIndex:'runs',align:'right',render:(value:unknown)=>n(value)},
       {title:'模型请求（成功 / 失败）',key:'calls',render:(_:unknown,row:Row)=>row.model_calls===undefined?<span className="muted">不调用模型</span>:`${n(row.ok)} / ${n(row.errors)}`},
       {title:'失败率',dataIndex:'error_rate',align:'right',render:(value:unknown)=>pct(value)},
       {title:'耗时中位',dataIndex:'median_ms',align:'right',render:(value:unknown)=>duration(value)},
       {title:'耗时 p90',dataIndex:'p90_ms',align:'right',render:(value:unknown)=>duration(value)},
       {title:'说明',key:'detail',render:(_:unknown,row:Row)=>stepDetail(row)},
      ]}/>
     {Object.keys(data.other_model_calls||{}).length>0&&<p className="muted">其他模型请求：{Object.entries(data.other_model_calls).map(([purpose,count])=>`${purpose} ${n(count)}`).join(' · ')}</p>}
    </Panel>
    <div className="two-col">
     <Panel title="一轮为什么失败">
      {(data.failures||[]).length?<ol className="view-rules">{(data.failures as Row[]).map(item=><li key={item.reason}>
       <strong>{item.reason} · {n(item.count)} 次</strong>
       {item.examples?.length>0&&<ul className="view-examples">{item.examples.map((example:Row)=><li key={example.detail}>{example.detail}（{n(example.count)}）</li>)}</ul>}
      </li>)}</ol>:<p className="muted">这段时间没有失败的轮次。</p>}
      {Object.keys(data.failure_phases||{}).length>0&&<p className="muted">失败发生在：{Object.entries(data.failure_phases).map(([phase,count])=>`${phaseLabels[phase]||phase} ${n(count)}`).join(' · ')}</p>}
     </Panel>
     <Panel title="每小时（悉尼）">
      <Table<Row> size="small" rowKey="hour" dataSource={data.hourly||[]} pagination={false}
       columns={[{title:'小时',dataIndex:'hour',render:(value:string)=>clock(value)},
        {title:'完成',dataIndex:'completed',align:'right'},{title:'失败',dataIndex:'failed',align:'right'},
        {title:'原创',dataIndex:'post',align:'right'},{title:'回复',dataIndex:'reply',align:'right'}]}/>
     </Panel>
    </div>
    <Panel title="每个角色">
     <Table<Row> size="small" rowKey="agent_id" dataSource={data.roles||[]} pagination={false} scroll={{x:720}}
      columns={[{title:'角色',dataIndex:'name',render:(value:string,row:Row)=><Link to={`/ai/agents/${row.agent_id}`}>{value}</Link>},
       {title:'完成',dataIndex:'completed',align:'right'},{title:'失败',dataIndex:'failed',align:'right'},
       {title:'原创',dataIndex:'post',align:'right'},{title:'回复',dataIndex:'reply',align:'right'},
       {title:'模型请求',dataIndex:'model_calls',align:'right'},
       {title:'模型失败',dataIndex:'model_errors',align:'right',render:(value:number)=>value?<Tag color="warning">{n(value)}</Tag>:'0'}]}/>
    </Panel>
    <ul className="view-notes">{(data.notes||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>
   </div>;
  }}</QueryState>
 </div>;
}

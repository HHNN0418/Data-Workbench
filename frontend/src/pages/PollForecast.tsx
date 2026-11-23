import {Alert, Collapse, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, QueryState, fmt, time} from '../common';

// Read-only: hourly Jev voting intention of known NPCs, calibrated on Poll 1 (vote_tracker, D74).
const choices:Record<string,string>={yes:'Yes',no:'No',unsure:'没决定'};
const pct=(value:unknown)=>typeof value==='number'?`${value.toFixed(1)}%`:'—';
const chance=(value:unknown)=>typeof value==='number'?`${Math.round(value*100)}%`:'—';
const shares=(row:Row|undefined)=>row?['yes','no','unsure'].map(key=>`${choices[key]} ${pct(row[key])}`).join(' · '):'—';
const statusText:Record<string,string>={no_jev_connection:'还没有可用的 Jev 连接，请在设置里添加 TypeSafe 连接',waiting:'正在判断第一批 NPC',not_run:'还没有运行过；服务启动约 2 分钟后开始'};

export default function PollForecast(){
 const query=useApi<Row>('/polls/forecast',{},300000);
 return <section className="panel stack" aria-label="投票意向预测">
  <div className="toolbar"><h3>投票意向与 Poll 2 预测（已知 NPC，按 Poll 1 校准）</h3></div>
  <QueryState query={query}>{data=>{
   if(data.status!=='ok')return <Alert type="info" showIcon title={statusText[data.status]||data.status}/>;
   const forecast=data.forecast||{},outcomes=forecast.outcomes||{},change=data.changes||{};
   const history:Row[]=[...(data.history||[])].reverse().slice(0,24);
   return <div className="stack">
    <div className="toolbar"><span className="muted">最新窗口截至 {time(data.latest?.window?.[1])} · {fmt(data.latest?.people)} 人 · 模型 {data.model} · 计算于 {time(data.generated_at)}</span></div>
    <Metrics items={[
     {label:'Poll 2 预测：表态者中 Yes',value:pct(forecast.yes_of_decided),hint:`90% 区间 ${pct(forecast.range_90?.[0])} – ${pct(forecast.range_90?.[1])}`},
     {label:'Yes 明显领先（+6000）',value:chance(outcomes.yes_lead)},
     {label:'太接近（+3000）',value:chance(outcomes.close_call)},
     {label:'No 明显领先（0）',value:chance(outcomes.no_lead)},
    ]}/>
    <div className="muted">校准后：{shares(forecast.shares)}　|　官方 Poll 1：{shares(data.poll1?.shares)}</div>
    <div className="muted">Jev 原始（概率加权）：Poll 1 之前 {shares(data.baseline?.probability)}；最新 {shares(data.latest?.probability)}</div>
    <div><strong>同一批人的变化</strong>（两段时间都发过帖的 {fmt(change.people)} 人）：
     {['yes','no','unsure'].map(key=><Tag key={key} color={key==='yes'?'green':key==='no'?'red':undefined}>{choices[key]} {change.net?.[key]>0?'+':''}{fmt(change.net?.[key])}</Tag>)}</div>
    {data.persuasion&&<div><strong>说服效果</strong>（Poll 1 之后被我们回复过的人 vs 其他人，按人数比例）：
     {[['replied','我们回复过'],['not_replied','没回复过']].map(([key,text])=>{const group=data.persuasion[key]||{};const shift=group.shift_points||{};
      return <Tag key={key}>{text} {fmt(group.people)} 人：Yes {shift.yes>0?'+':''}{fmt(shift.yes)} 点 · No {shift.no>0?'+':''}{fmt(shift.no)} 点</Tag>;})}</div>}
    <Collapse size="small" items={[
     {key:'history',label:'每小时预测',children:<Table<Row> size="small" rowKey="window_end" dataSource={history} pagination={false}
      columns={[{title:'窗口截至',dataIndex:'window_end',render:(value:string)=>time(value)},
       {title:'表态者中 Yes',dataIndex:'yes_of_decided',render:pct},
       {title:'90% 区间',dataIndex:'range_90',render:(value:number[])=>`${pct(value?.[0])} – ${pct(value?.[1])}`},
       {title:'人数',dataIndex:'people'}]}/>},
     {key:'transitions',label:'立场变化明细',children:<Table<Row> size="small" rowKey={row=>`${row.from}-${row.to}`} dataSource={change.transitions||[]} pagination={false}
      columns={[{title:'之前',dataIndex:'from',render:(value:string)=>choices[value]||value},{title:'最新',dataIndex:'to',render:(value:string)=>choices[value]||value},{title:'人数',dataIndex:'count'}]}/>},
     {key:'method',label:'方法',children:<ul className="muted identity-caveats">{(data.method||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>},
    ]}/>
   </div>;
  }}</QueryState>
 </section>;
}

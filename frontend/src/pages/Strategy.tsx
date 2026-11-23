import {Link} from 'react-router-dom';
import {Alert, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Panel, QueryState, time} from '../common';
import {n, pct, ratio} from './pipelineModel';
import {actionLabels, concernLabel} from './stageViews';
import './stageViews.css';

// The rules in force, read from the code constants the Agents act on (GET /api/strategy/rules). Read-only.
const groupNames:Record<string,string>={health:'健康类顾虑',economy:'经济类顾虑',government:'政府类顾虑'};

export function Rules({rules}:{rules:Row[]}) {
 return <ul className="view-rules">{rules.map(rule=><li key={rule.key}><strong>{rule.title}</strong><span>{rule.text}</span></li>)}</ul>;
}

function HourGrid({limits,peaks}:{limits:Array<number|null>;peaks:boolean[]}) {
 return <div className="view-hours" role="img" aria-label={`在班 ${limits.filter(value=>value!==null).length} 个小时`}>
  {limits.map((limit,hour)=><span key={hour} title={`${hour}:00 ${limit===null?'休息':`每小时最多 ${limit} 条`}`}
   className={`view-hour${limit!==null?' view-hour-on':''}${peaks[hour]?' view-hour-peak':''}`}>{limit??''}</span>)}
 </div>;
}

const minutes=(value:string)=>{const [h,m]=value.split(':').map(Number);return h*60+m;};
function peakHours(peaks:Row[]):boolean[] {
 return Array.from({length:24},(_,hour)=>peaks.some(peak=>{const start=minutes(peak.start),end=minutes(peak.end),at=hour*60;return start<=end?at>=start&&at<end:at>=start||at<end;}));
}

export default function Strategy() {
 const query=useApi<Row>('/strategy/rules',{},60000);
 return <div className="stack">
  <Alert type="info" showIcon title="只读：Agent 现在按这些规则选动作和目标" description="数值直接读自代码里 Agent 使用的常量；这里不能修改。角色的可选动作和班次以外的设置在「角色」和「队伍设置」里改。"/>
  <QueryState query={query}>{data=>{
   const schedule=data.schedule||{},mix=data.recent_mix||{},figures=data.public_figures||{},peaks=peakHours(schedule.peaks||[]);
   const allowed=data.allowed_actions||{};
   return <div className="stack">
    <div className="toolbar"><span className="muted">读取于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <div className="two-col">
     <Panel title="发帖与回复的配比" extra={<Link to="/agents/pipeline">流水线监控</Link>}>
      <div className="stack">
       <Rules rules={data.mix||[]}/>
       <p className="muted">最近 {n(mix.actions)} 条发布：原创 {n(mix.posts)}、回复 {n(mix.replies)}（原创占 {pct(mix.post_share)}，每条回复配 {ratio(mix.originals_per_reply)} 条原创）</p>
       <p className="muted">现在启用的 {n(allowed.roles_enabled)} 个角色可选：{Object.entries(allowed.counts||{}).map(([action,count])=>`${actionLabels[action]||action}（${n(count)} 个角色）`).join('、')||'—'}</p>
      </div>
     </Panel>
     <Panel title="只回复市民：目标规则" extra={<Link to="/processing/bots">账号识别</Link>}><Rules rules={data.targeting||[]}/></Panel>
    </div>
    <Panel title="原创帖 @ 公众人物">
     <div className="stack">
      <p>{figures.rule}</p>
      <Table<Row> size="small" rowKey="username" dataSource={figures.figures||[]} pagination={false} scroll={{x:760}}
       columns={[{title:'账号',dataIndex:'username',render:(value:string)=>`@${value}`},
        {title:'是谁',dataIndex:'who'},
        {title:'负责什么',dataIndex:'how'},
        {title:'优先用于',key:'first',render:(_:unknown,row:Row)=><div>{(row.first_for||[]).map((group:string)=><Tag key={group}>{groupNames[group]||group}</Tag>)}
         <div className="view-cell-sub">{(row.concerns||[]).map(concernLabel).join('、')||'—'}</div>
         {(row.second_for||[]).length>0&&<div className="view-cell-sub">备选：{row.second_for.map((group:string)=>groupNames[group]||group).join('、')}</div>}</div>}]}/>
     </div>
    </Panel>
    <Panel title={`班次（${schedule.timezone==='Australia/Sydney'?'悉尼时间':schedule.timezone||'—'}）`} extra={<Link to="/ai/agents">角色</Link>}>
     <div className="stack">
      <p className="muted">{schedule.custom?'使用队伍设置里保存的班次。':'使用代码里的默认班次。'}格子里的数字是该小时每个账号最多的原创加回复条数；橙框是市民高峰（{(schedule.peaks||[]).map((peak:Row)=>`${peak.start}–${peak.end}`).join('、')}）。{schedule.peak_now?'现在是高峰。':''}班次结束后 {n((schedule.publish_grace_seconds||0)/60)} 分钟内，已生成的回复仍可发出。</p>
      <Table<Row> size="small" rowKey="group" dataSource={schedule.groups||[]} pagination={false} scroll={{x:980}}
       columns={[{title:'组',dataIndex:'group',render:(value:string)=><strong>{value}</strong>},
        {title:'成员',dataIndex:'members',render:(members:Row[])=>members.map(member=>member.name||`#${member.account_id}`).join('、')},
        {title:'现在',key:'now',render:(_:unknown,row:Row)=>row.on_shift?<Tag color="green">在班 · 每小时 {n(row.current_limit)} 条</Tag>:<Tag>休息</Tag>},
        {title:<div className="view-hour-axis">{Array.from({length:24},(_,hour)=><span key={hour}>{hour}</span>)}</div>,key:'hours',
         render:(_:unknown,row:Row)=><HourGrid limits={row.hour_limits||[]} peaks={peaks}/>}]}/>
      {(schedule.unscheduled||[]).length>0&&<p className="muted">不在任何组、不受班次限制的角色：{schedule.unscheduled.join('、')}</p>}
     </div>
    </Panel>
   </div>;
  }}</QueryState>
 </div>;
}

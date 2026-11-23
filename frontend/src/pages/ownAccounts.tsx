import {Alert, Tag, Tooltip} from 'antd';
import {Link} from 'react-router-dom';
import {useApi, type Row} from '../api';
import './ownAccounts.css';

// Our accounts as our own rules see them (GET /api/accounts/identity?label=ours): roles near suspension by hidden
// posts (own_risks, hidden_posts) and what the bot / non-citizen rules would call each account (rule_label).
// Risk information only: never used to tune what the Agents write to avoid detection (D47).
export const labels:Record<string,{text:string,color?:string}>={citizen:{text:'市民 NPC',color:'green'},bot:{text:'机器人',color:'red'},non_citizen:{text:'高频 / 非市民',color:'orange'},ours:{text:'我方 Agent',color:'blue'},teammate:{text:'我方 · 队友管理',color:'geekblue'},public_figure:{text:'公众人物 / 媒体',color:'purple'}};
export const reasonText:Record<string,string>={behavior:'行为规则：72 小时内回答了一半以上收到的回复，或多次在很深的楼层回帖',always_on:'24 小时里有 20 个小时以上在发帖',high_volume:'一天超过 60 条，或某个小时超过 12 条',fast_reply:'回复的中位时间不到 2 分钟',regular_rhythm:'发帖间隔非常规律',heavy:'已采集动作 25 条以上，而且 48 小时内超过 20 条',repost_ring:'一半以上是转发（转发圈）',verified:'已认证账号',profile:'资料不像平台 NPC：没有显示名、显示名和用户名相同、用户名带空格或十六进制后缀，或者显示名不是 2–3 个首字母大写的词',style:'文风不像 NPC：最近 20 条里问号、超过 280 字、hashtag 很多',batch_name_or_artefact:'批量编号的用户名或 AI 套话',calibrated:'官方小时校准：在官方记为 0 条 NPC 回复的小时里回复过我们，确定不是 NPC'};

export type OwnRisk={account_id:number;name:string;hidden:number;actions:number;level:'suspend_next'|'report';text:string};
const riskShort:Record<string,string>={suspend_next:'再隐藏 1 条可能封号',report:'再被举报可能封号'};
const riskRule='3 条帖子被平台永久隐藏就封号；已有 1 条被永久隐藏、操作满 100 次的账号，被举报一次也可能封号。公开数据分不清临时隐藏和永久隐藏，所以这里是偏保守的估计；角色自己删掉的帖子不算。操作次数取我们自己的操作记录（发帖、回复、转发、点赞、关注等）。每小时核对一次最近 48 小时的帖子。';

// One request for all our accounts; the server keeps the result five minutes.
export function useOwnAccounts() {
 const data=useApi<Row>('/accounts/identity',{label:'ours',limit:1000},300000,true).data;
 const risks:OwnRisk[]=Array.isArray(data?.own_risks)?data!.own_risks:[];
 const rows:Row[]=Array.isArray(data?.rows)?data!.rows:[];
 return {risks,risk:(id?:number|null)=>risks.find(risk=>risk.account_id===id),identity:(id?:number|null)=>rows.find(row=>row.account_id===id),loaded:!!data};
}

export function RiskTag({risk}:{risk?:OwnRisk}) {
 return risk?<Tooltip title={risk.text}><Tag color={risk.level==='suspend_next'?'red':'orange'}>⚠ {riskShort[risk.level]||'封号风险'}</Tag></Tooltip>:null;
}

export function RiskAlert({risk}:{risk?:OwnRisk}) {
 return risk?<Alert type="warning" showIcon title={risk.text} description={riskRule}/>:null;
}

// The at-risk roles in one box, for lists.
export function RiskSummary({risks}:{risks:OwnRisk[]}) {
 return risks.length?<Alert type="warning" showIcon title={`${risks.length} 个账号有封号风险`} description={<><ul className="own-risk-list">{risks.map(risk=><li key={risk.account_id}><Link to={`/accounts?q=${risk.account_id}`}>{risk.text}</Link></li>)}</ul><span className="muted">{riskRule}</span></>}/>:null;
}

function reasonList(reasons:string[]) {return reasons.map(reason=>reasonText[reason]||reason);}

// What our identification rules would call this account: 市民 NPC, 高频 / 非市民 or 机器人.
export function IdentityTag({row}:{row?:Row}) {
 if(!row?.rule_label)return null;
 const label=labels[row.rule_label]||{text:row.rule_label},reasons=reasonList(row.rule_reasons||[]);
 return <Tooltip title={reasons.length?reasons.join('；'):'没有触发机器人或非市民规则'}><Tag color={label.color}>系统识别：{label.text}</Tag></Tooltip>;
}

export function IdentityDetail({row,accountId}:{row?:Row;accountId?:number|null}) {
 if(!accountId)return null;
 const link=<Link to={`/processing/bots?label=ours&q=${accountId}`}>账号识别 ↗</Link>;
 if(!row?.rule_label)return <section className="own-identity" aria-label="系统识别"><div className="agents-section-head"><h4>系统识别</h4>{link}</div><p className="muted">最近 24 小时没有活动，暂时没有识别结果。</p></section>;
 const reasons=reasonList(row.rule_reasons||[]);
 return <section className="own-identity" aria-label="系统识别"><div className="agents-section-head"><h4>系统识别</h4>{link}</div>
  <p><IdentityTag row={row}/>按我们自己的机器人和非市民规则，这个账号会被识别成「{(labels[row.rule_label]||{text:row.rule_label}).text}」。</p>
  {reasons.length?<ul className="own-identity-reasons">{reasons.map(reason=><li key={reason}>{reason}</li>)}</ul>:<p className="muted">没有触发任何机器人或非市民规则。</p>}
  <p className="muted">别的队如果用类似的规则找机器人，被判为机器人或高频的账号更容易被举报。只用来看风险，不用来调整发帖去躲避识别（D47）。</p>
 </section>;
}

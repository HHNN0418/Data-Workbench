import type {Row} from '../api';
import type {Tone} from '../ui/status';

// Presentation of /api/pipeline/status (docs/PIPELINE_UI_SPEC.md §3–4, docs/handoff/C-api.md §1). Status and
// thresholds come from the backend; this file only picks the numbers to show, explains a status and sets the links.
export const stageOrder=['collect','process','analyze','strategy','execute','outcome'] as const;
export type StageKey=typeof stageOrder[number];
export type StageStatus='ok'|'warn'|'bad'|'unknown';

export const stageSteps:Record<StageKey,string>={collect:'①',process:'②',analyze:'③',strategy:'④',execute:'⑤',outcome:'⑥'};
export const stageLinks:Record<StageKey,string>={collect:'/operations',process:'/processing',analyze:'/analysis',strategy:'/strategy',execute:'/agents/pipeline',outcome:'/score'};
// Sidebar groups that carry each stage's status dot (`outcome` is the sidebar's results group).
export const stageGroups:Record<StageKey,string>={collect:'collect',process:'process',analyze:'analyze',strategy:'strategy',execute:'execute',outcome:'results'};
export const statusText:Record<string,string>={ok:'正常',warn:'注意',bad:'异常',unknown:'读取失败'};

export function stageTone(status:unknown):Tone {return status==='ok'?'ok':status==='warn'?'warn':status==='bad'?'danger':'neutral';}
export const statusRank:Record<string,number>={bad:3,warn:2,unknown:1,ok:0};

const compact=new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1});
const plain=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1});
export function num(value:unknown):number|null {if(value===null||value===undefined||value==='')return null;const v=Number(value);return Number.isFinite(v)?v:null;}
// null means "nothing to compute", never 0 (C-api.md): it shows as a dash.
export function n(value:unknown,short=false):string {const v=num(value);return v===null?'—':short?compact.format(v):plain.format(v);}
export function pct(value:unknown):string {const v=num(value);return v===null?'—':`${plain.format(v*100)}%`;}
// Ratios such as the 0.25 post:reply threshold need two decimals; one would show it as 0.3.
const fine=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2});
export function ratio(value:unknown):string {const v=num(value);return v===null?'—':fine.format(v);}

export interface NodeView {main:string;mainLabel:string;details:string[]}
// One headline number and a few supporting numbers per stage.
export function nodeView(key:StageKey,m:Row={}):NodeView {
 switch(key){
  case 'collect':return {main:n(m.posts_total,true),mainLabel:'帖子',details:[`+${n(m.posts_last_hour)} / 小时`,`Latest ${n(m.feed_age_minutes)} 分钟前`,`积压任务 ${n(m.task_backlog)}`]};
  case 'process':return {main:n(m.annotated_posts,true),mainLabel:'已标注',details:[`标注积压 ${n(m.annotation_backlog)}`,`疑似机器人 ${n(m.bot_accounts)}`,`待审事实 ${n(m.pending_facts)}`]};
  case 'analyze':return {main:n(m.undecided_posts_6h),mainLabel:'犹豫帖 / 6 小时',details:[`Yes 占比 ${pct(m.yes_share_6h)}`,m.radar_age_hours==null?'还没有雷达':`雷达 ${n(m.radar_age_hours)} 小时前`,...(m.blue_plan_today!=null?[`Blue 今日建议 ${n(m.blue_plan_today)}`]:[])]};
  case 'strategy':return {main:n(m.active_facts),mainLabel:'生效事实',details:[`原创占比 ${pct(m.post_share)}`,`不可作目标 ${pct(m.target_blocked_share)}`,`目标池 ${n(m.target_pool_6h)} / 6 小时`]};
  case 'execute':return {main:n(m.published_last_hour),mainLabel:'发布 / 小时',details:[`决策 ${n(m.decisions_last_hour)} / 小时`,`原创:回复 ${n(m.posts_6h)}:${n(m.replies_6h)}`,`失败率 ${pct(m.failure_rate_6h)}`,`未确认通知 ${n(m.inbox_unresolved)}`]};
  case 'outcome':return {main:n(m.points_per_hour_6h),mainLabel:'分 / 小时（6h）',details:[`需要 ${n(m.required_points_per_hour)} 分/时`,`今日浏览 ${n(m.views_today)} / ${n(m.view_cap)}`,`向我方转变 ${n(m.changes_towards_6h)} / 6 小时`,`市民回复 ${n(m.citizen_replies_6h)} / 共 ${n(m.replies_to_us_6h)}`]};
 }
}

// What flows along the arrow that leaves each stage.
export function flowLabel(from:StageKey,stages:Partial<Record<StageKey,Row>>):string {
 const m=(key:StageKey)=>stages[key]?.metrics||{};
 switch(from){
  case 'collect':return `+${n(m('collect').posts_last_hour)} 帖/时`;
  case 'process':return `公民帖 ${n(m('analyze').citizen_posts_6h)} / 6h`;
  case 'analyze':return `目标池 ${n(m('strategy').target_pool_6h)} / 6h`;
  case 'strategy':return `决策 ${n(m('execute').decisions_6h)} / 6h`;
  case 'execute':return `发布 ${n(m('execute').published_6h)} / 6h`;
  default:return '';
 }
}

// The stage to look at first: the worst status, earliest in the flow on ties. Unreadable stages are shown on
// their own node but are not called a bottleneck.
export function bottleneck(stages:Row[]):Row|undefined {
 return stages.filter(stage=>stage.status==='bad'||stage.status==='warn').sort((a,b)=>(statusRank[b.status]||0)-(statusRank[a.status]||0)||stageOrder.indexOf(a.key)-stageOrder.indexOf(b.key))[0];
}

type Level='warn'|'bad'|null;
function crossed(value:unknown,limits:unknown,lowerIsWorse=false):Level {
 const v=num(value);
 if(v===null||!Array.isArray(limits)||limits.length<2)return null;
 const warn=Number(limits[0]),bad=Number(limits[1]);
 if(lowerIsWorse)return v<bad?'bad':v<warn?'warn':null;
 return v>bad?'bad':v>warn?'warn':null;
}
function said(level:Exclude<Level,null>,lowerIsWorse:boolean,limit:string) {return `，${lowerIsWorse?'低于':'超过'} ${limit} ${level==='bad'?'判为异常':'需要注意'}`;}

// Why a stage is yellow or red, one sentence per metric past the backend's thresholds (C-api.md §1.3–1.4).
export function stageReasons(key:StageKey,m:Row={},thresholds:Row={}):string[] {
 const out:string[]=[];
 const check=(name:string,label:string,shown:(value:unknown)=>string,lowerIsWorse=false)=>{
  const level=crossed(m[name],thresholds[name],lowerIsWorse);
  if(level)out.push(`${label} ${shown(m[name])}${said(level,lowerIsWorse,shown(thresholds[name][level==='bad'?1:0]))}`);
 };
 const minutes=(value:unknown)=>`${n(value)} 分钟`,hours=(value:unknown)=>`${n(value)} 小时`,count=(value:unknown)=>n(value);
 switch(key){
  case 'collect':
   if(m.feed_age_minutes==null)out.push('24 小时内没有 Latest 采集记录');
   check('feed_age_minutes','距最近一次 Latest 采集',minutes);
   check('counter_age_minutes','距最近一次计数快照',minutes);
   check('task_backlog','到期未完成的采集任务',count);
   break;
  case 'process':
   check('annotation_backlog','标注积压',count);
   if(m.fact_extraction_enabled)check('fact_extraction_age_hours','距最近一次事实提取',hours);
   break;
  case 'analyze':
   if(m.radar_age_hours==null)out.push('还没有顾虑雷达报告');
   check('radar_age_hours','顾虑雷达生成于',value=>`${n(value)} 小时前`);
   break;
  case 'strategy':
   check('post_share',`最近 ${n(m.recent_actions)} 次发布里原创占比`,pct,true);
   check('target_blocked_share','6 小时目标池里不能回复的比例',pct);
   break;
  case 'execute':{
   check('failure_rate_6h','6 小时决策失败率',pct);
   const top=(m.top_failures_6h||[])[0];
   if(top&&crossed(m.failure_rate_6h,thresholds.failure_rate_6h))out.push(`主要失败原因：${top.reason}（${n(top.count)} 次）`);
   const idle=num(thresholds.idle_hours),since=num(m.minutes_since_publish);
   if(idle!==null&&(since===null||since>idle*60))out.push(since===null?'24 小时内没有发布':`已经 ${n(since/60)} 小时没有发布（队伍开着时超过 ${n(idle)} 小时判为异常）`);
   // The 2:1 post/reply rule was cancelled (D52): the mix is shown on the card, not judged.
   break;
  }
  case 'outcome':{
   const need=num(m.required_points_per_hour),h12=num(m.points_per_hour_12h),h6=num(m.points_per_hour_6h);
   if(need&&h12!==null&&h12<need)out.push(`最近 12 小时 ${n(h12)} 分/时，低于 Poll ${n(m.next_poll)} 需要的 ${n(need)} 分/时`);
   else if(need&&h6!==null&&h6<need)out.push(`最近 6 小时 ${n(h6)} 分/时，低于 Poll ${n(m.next_poll)} 需要的 ${n(need)} 分/时（12 小时还够）`);
   if(m.score_stale)out.push('积分快照过旧，速度可能不准');
   break;
  }
 }
 return out;
}

// Sidebar dots: each stage on its group, the overall status on the home item.
export function sidebarStatus(data:Row|undefined):Record<string,{status:string;headline?:string}> {
 if(!data)return {};
 const out:Record<string,{status:string;headline?:string}>={};
 for(const stage of data.stages||[])if(stageGroups[stage.key as StageKey])out[stageGroups[stage.key as StageKey]]={status:stage.status,headline:stage.headline};
 if(data.overall)out.overview={status:data.overall};
 return out;
}

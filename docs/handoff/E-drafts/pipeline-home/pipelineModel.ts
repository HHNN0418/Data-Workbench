import type {Row} from '../api';
import type {Tone} from '../ui/status';

// Presentation of C's /api/pipeline/status (docs/PIPELINE_UI_SPEC.md §3–4). Thresholds live in the backend;
// this file only chooses which metrics to show and where each node links.
export const stageOrder=['collect','process','analyze','strategy','execute','outcome'] as const;
export type StageKey=typeof stageOrder[number];

export const stageLinks:Record<StageKey,string>={collect:'/operations',process:'/processing',analyze:'/analysis',strategy:'/strategy',execute:'/agents/pipeline',outcome:'/score'};
// Sidebar groups that carry each stage's status dot.
export const stageGroups:Record<StageKey,string>={collect:'collect',process:'process',analyze:'analyze',strategy:'strategy',execute:'execute',outcome:'results'};

export function stageTone(status:unknown):Tone {return status==='ok'?'ok':status==='warn'?'warn':status==='bad'?'danger':'neutral';}
export const statusRank:Record<string,number>={bad:3,warn:2,unknown:1,ok:0};

const count=new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1});
const plain=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1});
function n(value:unknown,compact=false):string {const v=Number(value);return value===null||value===undefined||!Number.isFinite(v)?'—':compact?count.format(v):plain.format(v);}
function pct(value:unknown):string {const v=Number(value);return value===null||value===undefined||!Number.isFinite(v)?'—':`${plain.format(v*100)}%`;}

export interface NodeView {main:string;mainLabel:string;details:string[]}
// One headline number and up to three supporting numbers per stage.
export function nodeView(key:StageKey,m:Row={}):NodeView {
 switch(key){
  case 'collect':return {main:n(m.posts_total,true),mainLabel:'帖子',details:[`+${n(m.posts_last_hour)} / 小时`,`Latest 延迟 ${n(m.feed_age_minutes)} 分钟`,`积压任务 ${n(m.task_backlog)}`]};
  case 'process':return {main:n(m.annotated_posts,true),mainLabel:'已标注',details:[`标注积压 ${n(m.annotation_backlog)}`,`疑似机器人 ${n(m.bot_accounts)}`,`待审事实 ${n(m.pending_facts)}`]};
  case 'analyze':return {main:n(m.undecided_posts_6h),mainLabel:'犹豫帖 / 6 小时',details:[`Yes 占比 ${pct(m.yes_share_6h)}`,`雷达 ${n(m.radar_age_hours)} 小时前`,...(m.blue_plan_today!=null?[`Blue 今日建议 ${n(m.blue_plan_today)}`]:[])]};
  case 'strategy':return {main:n(m.active_facts),mainLabel:'生效事实',details:[`原创占比 ${pct(m.post_share)}`,`不可作目标 ${pct(m.target_blocked_share)}`,`目标池 ${n(m.target_pool_6h)} / 6 小时`]};
  case 'execute':return {main:n(m.published_last_hour),mainLabel:'发布 / 小时',details:[`决策 ${n(m.decisions_last_hour)} / 小时`,`失败率 ${pct(m.failure_rate_6h)}`,`未确认通知 ${n(m.inbox_unresolved)}`]};
  case 'outcome':return {main:n(m.points_per_hour_6h),mainLabel:'分 / 小时（6h）',details:[`需要 ${n(m.required_points_per_hour)} 分/时`,`12h ${n(m.points_per_hour_12h)} 分/时`,`向我方转变 ${n(m.changes_towards_6h)} / 6 小时`]};
 }
}

// What flows along each arrow, from the left stage's point of view.
export function flowLabel(from:StageKey,stages:Partial<Record<StageKey,Row>>):string {
 const m=(key:StageKey)=>stages[key]?.metrics||{};
 switch(from){
  case 'collect':return `+${n(m('collect').posts_last_hour)}/时`;
  case 'process':return `公民帖 ${n(m('analyze').citizen_posts_6h)}/6h`;
  case 'analyze':return `目标池 ${n(m('strategy').target_pool_6h)}`;
  case 'strategy':return `决策 ${n(m('execute').decisions_6h)}/6h`;
  case 'execute':return `发布 ${n(m('execute').published_6h)}/6h`;
  default:return '';
 }
}

// The stage to look at first: the worst status, earliest in the flow on ties.
export function bottleneck(stages:Row[]):Row|undefined {
 return stages.filter(stage=>stage.status==='bad'||stage.status==='warn').sort((a,b)=>(statusRank[b.status]||0)-(statusRank[a.status]||0)||stageOrder.indexOf(a.key)-stageOrder.indexOf(b.key))[0];
}

import {useEffect, useRef, useState,type ReactNode} from 'react';
import {Link,useParams,useSearchParams} from 'react-router-dom';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {Alert, Button, Checkbox, Collapse, Drawer, Empty, Form, Input, InputNumber, Modal, Pagination, Select, Skeleton, Space, Switch, Table, Tabs, Tag, Tooltip, message} from 'antd';
import {request, useAction, useApi, type Row} from '../api';
import {useAIJob} from '../useAIJob';
import {time} from '../common';
import AgentLogs,{AgentRelationships} from './AgentLogs';
import AgentManualActions from './AgentManualActions';
import AgentQuotas from './AgentQuotas';
import AgentCompetitionTrial from './AgentCompetitionTrial';
import AgentResearchReference from './AgentResearchReference';
import AgentMemoryHistory,{MemorySources,type MemorySource} from './AgentMemoryHistory';
import {reviewClauseLabel,reviewLanguageLabel} from './AIReview';
import {IdentityDetail,IdentityTag,RiskAlert,RiskSummary,RiskTag,useOwnAccounts} from './ownAccounts';
import './agents.css';

type ActionType = 'post'|'reply'|'repost'|'like'|'unlike'|'follow'|'unfollow'|'skip'|'delete_post';
type InboxItem = {id:number|string;notification_id?:number;post_id?:number|null;kind?:string;status?:string;created_at?:string;observed_at?:string;resolved_at?:string;source_id?:string|null;local_progress?:string;related_draft_id?:string|null;related_action?:ActionType|null;related_draft_state?:string|null;can_confirm_skip?:boolean;can_reconsider?:boolean;policy_ignored?:boolean;policy_reason?:string};
type ReadingSelection = {status?:'selected'|'empty'|'fallback';cache_hit?:boolean;candidate_count?:number;selected_count?:number;reason?:string;error?:unknown};
type ThreadContext = {requested_count?:number;included_count?:number;background_count?:number;missing_count?:number;deferred_count?:number};
type Observation = {observed_at?:string;last_refresh_at?:string;notifications_refreshed_at?:string;home_refreshed_at?:string;next_refresh_at?:number|string;latest_post_at?:string;latest_news_at?:string;source_count?:number;new_source_count?:number;unresolved_count?:number;notifications_status?:string;home_status?:string;error?:string;reading_selection?:ReadingSelection;thread_context?:ThreadContext};
type MemoryEntry = {id?:string|number;kind?:string;at?:number|string;created_at?:string;title?:string;excerpt?:string;source_id?:string;source_ids?:string[];evidence?:MemorySource[];post_id?:number;author_id?:number;action?:string;body?:string;state?:string;status?:string;reason?:string;draft_id?:string;target_post_id?:number;target_account_id?:number;target_author_id?:number;origin?:string;read_count?:number;last_read_at?:string;already_applied?:boolean;note_source?:string};
type MemoryView = {observed_reading?:MemoryEntry[];completed_actions?:MemoryEntry[];pending_intentions?:MemoryEntry[];uncertain_actions?:MemoryEntry[];relationships?:MemoryEntry[];subjective_notes?:MemoryEntry[]|string;long_term?:{counts?:Record<string,number>;total?:number;retention?:string;latest_at?:string}};
type Generation = {id?:string;status?:'queued'|'running'|'cancelling'|'waiting'|'completed'|'failed'|'cancelled';started_at?:number|string;updated_at?:number|string;stage?:string;elapsed_seconds?:number;error?:string;waiting_for?:'publish'|'decision';wait_kind?:string;wait_message?:string;retry_at?:number;draft_id?:string};
type FeedbackCounts = {like_count?:number|null;reply_count?:number|null;repost_count?:number|null;view_count?:number|null};
type FeedbackPost = {post_id:number;action?:string|null;draft_id?:string|null;body?:string;created_at?:string;latest_counts?:FeedbackCounts;sampled_at?:string|null;stale?:boolean|null;remote_deleted_at?:string|null;interaction_count?:number|null};
type FeedbackAction = {draft_id:string;action:string;status:string;post_id?:number|null;target_post_id?:number|null;target_account_id?:number|null;performance_post_id?:number|null;performance_scope?:'own_post'|'target_context'|'unknown'|'deletion';remote_deleted_at?:string|null;origin?:string;performance?:FeedbackPost|null};
type Feedback = {account_id?:number|null;status?:string;generated_at?:string;known_post_count?:number|null;confirmed_action_count?:number;action_counts?:Partial<Record<ActionType,number>>;window?:{limit?:number;post_count?:number;sampled_posts?:number};metrics?:FeedbackCounts;metric_coverage?:Partial<Record<keyof FeedbackCounts,number>>;latest_sampled_at?:string|null;recent_posts?:FeedbackPost[];actions?:FeedbackAction[];score_estimate?:{status?:string;is_official?:boolean;points_decimal?:string|null;reason?:string};official?:{status?:string;points_decimal?:string|null;events?:Row[];team_snapshot?:{total_decimal?:string;observed_at?:string;source?:string}|null;reason?:string};notes?:string[]};
type Agent = {id:string;name:string;username:string;background?:string;stance?:string;voice?:string;interests?:string;language?:string;provider_id?:string;enabled:boolean;mode:'review'|'auto';daily_limit?:number;interval_seconds:number;allowed_actions?:ActionType[];freshness_minutes?:number;news_freshness_hours?:number;notification_interval_seconds?:number;account_id?:number|null;memory?:string;memory_view?:MemoryView;feedback?:Feedback;generation?:Generation;relationships?:Row;last_read?:string;last_run?:string;last_sources?:Source[];last_publish?:string;next_run?:string;error?:string;status?:string;busy?:boolean;busy_reason?:'generation'|'operation'|'lease_recovery'|'maintenance';busy_until?:number|string;account_state?:string;account_suspended?:{at?:string;username?:string;message?:string}|null;version?:number;inbox?:InboxItem[];observation?:Observation;activity?:Row[]};
type Source = {id?:string;title?:string;kind?:string;origin?:string;text?:string;body?:string;post_id?:number;url?:string;site?:string;created_at?:string;published_at?:string;first_seen?:string;observed_at?:string;notification_ids?:number[];context_only?:boolean;context_role?:string;thread_context?:{parent_post_id?:number|null;root_post_id?:number|null;missing?:{post_id:number|null;role:string;reason?:string}[];complete?:boolean}};
type Draft = {review?:Row;review_summary?:string;id:string;agent_id:string;account_id?:number;version?:number;body:string;action:ActionType;target_post_id?:number;target_account_id?:number;reply_to?:number;status:string;sources?:Source[];sources_omitted?:number;source_ids?:string[];notification_ids?:number[];created_at?:string;post_id?:number;reason?:string;last_error?:string;style_warnings?:string[];result?:unknown;remote_deleted_at?:string|null;manual?:boolean;origin?:string;manual_only?:boolean;manual_prepared?:boolean;manual_queue?:Row;manual_preview?:Row;requires_review?:boolean;review_hold_reason?:string;review_reason?:string;notifications_resolved?:boolean;notification_confirmation_authorized_at?:string;policy_blocked?:boolean;policy_reason?:string};
type Settings = {enabled:boolean;team_side:string;max_accounts:number;global_interval_seconds?:number;block_agent_interactions:boolean;rocket_mode?:boolean};
type AgentState = {settings:Settings;agents:Agent[];drafts:Draft[];events:Row[];has_team_code:boolean;sdk_available:boolean;running?:unknown;cooldown_until?:number|string;budgets?:Record<string,Row>;interaction_policy?:{enabled:boolean;blocked_today:number;managed_accounts:number};maintenance?:{active:boolean;reason?:string}};
type Provider = {id:string;name:string;model:string;protocol:string;enabled:boolean};
type Persona = {name?:string;username?:string;background?:string;stance?:string;voice?:string;interests?:string|string[];language?:string};
type GeneratedPersona = {persona:Persona;provider_id?:string};
const actionLabels:Record<ActionType,string> = {post:'发帖',reply:'回复',repost:'转发',like:'点赞',unlike:'取消点赞',follow:'关注',unfollow:'取消关注',skip:'跳过',delete_post:'删除本人帖子'};
const allActions = Object.keys(actionLabels).filter(action=>action!=='delete_post') as ActionType[];
const legacyActions:ActionType[]=['post','reply','repost','like','skip'];
const draftLabels:Record<string,string> = {draft:'待审阅',sending:'正在执行',published:'已完成',uncertain:'待核对',discarded:'已放弃',failed:'执行失败',skipped:'已跳过'};
const notificationProgressLabels:Record<string,string> = {awaiting_decision:'待判断',read_awaiting_decision:'已阅读，待判断',awaiting_review:'关联操作待审阅',queued:'关联操作待执行',executing:'关联操作执行中',needs_verification:'关联操作待核对',failed:'关联操作执行失败',completed:'关联操作已完成',skipped:'已确认跳过',skipped_awaiting_confirmation:'已决定跳过，待确认',discarded:'操作已放弃，待重新判断',reconsidered:'已重新开放判断',policy_ignored:'内部互动，已忽略'};
const sourceLabels:Record<string,string> = {manual_target:'手动选择目标',notification:'通知',home:'首页动态',recent:'近期帖子',thread:'对话上下文',news:'新闻'};
const observationLabels:Record<string,string> = {ok:'已更新',cached:'使用缓存',error:'更新失败',unavailable:'暂不可用',unbound:'未连接账号',deferred:'等待调度'};
const agentLabels:Record<string,string> = {idle:'等待运行',running:'正在运行',reading:'正在阅读',generating:'正在写作',publishing:'正在发布',error:'运行出错',paused:'已暂停',ready:'准备就绪'};
const budgetLabels:Record<string,string> = {post:'发帖、回复与转发',like:'点赞',unlike:'取消点赞',follow:'关注',unfollow:'取消关注',delete_post:'删除帖子',feed:'首页动态',read:'资料查询',auth:'账号连接',notification:'获取通知',resolve:'确认通知','auth:login':'登录账号','auth:register':'注册账号','read:get_post':'帖子详情','read:get_account':'账号资料','read:me':'本人账号资料','read:my_likes':'核对点赞关系','read:my_reposts':'核对转发关系','read:following':'关注列表','read:account_posts':'账号帖子','read:account_likes':'账号点赞列表'};
function budgetLabel(bucket:string){if(budgetLabels[bucket])return budgetLabels[bucket];if(bucket.startsWith('read:'))return `资料查询 · ${bucket.slice(5)}`;if(bucket.startsWith('auth:'))return `账号认证 · ${bucket.slice(5)}`;return `平台请求 · ${bucket}`;}
const emptyState:AgentState = {settings:{enabled:false,team_side:'Vote Yes',max_accounts:10,block_agent_interactions:true},agents:[],drafts:[],events:[],has_team_code:false,sdk_available:false};
const editableFields = ['name','username','background','stance','voice','interests','language','provider_id','enabled','mode','interval_seconds','allowed_actions','freshness_minutes','news_freshness_hours','notification_interval_seconds'];
const personaFields = ['name','username','background','stance','voice','interests','language'] as const;
function personaPayload(values:Row):Persona{return {...Object.fromEntries(personaFields.map(key=>[key,values[key]]).filter(([,value])=>value!==undefined)),language:'English'};}
function settingsPayload(settings:Settings){return {enabled:!!settings.enabled,team_side:settings.team_side||'Vote Yes',max_accounts:settings.max_accounts??10,block_agent_interactions:settings.block_agent_interactions??true};}
function notificationIgnored(item:InboxItem){return !!item.policy_ignored||item.local_progress==='policy_ignored'||item.status==='policy_ignored';}
function notificationUnfinished(item:InboxItem){return !notificationIgnored(item)&&['unresolved','ack_pending'].includes(item.status||'');}
function editablePayload(values:Row){return {...Object.fromEntries(editableFields.map(key=>[key,values[key]]).filter(([,value])=>value!==undefined)),language:'English'};}
function usernameIsBound(agent?:Agent|null){return !!agent?.account_id||!!agent?.account_state&&agent.account_state!=='unbound';}
function timestamp(value:unknown):string|undefined {if(typeof value==='number')return value>0?new Date(value*1000).toISOString():undefined;return typeof value==='string'&&value?value:undefined;}
function lastPublishedAt(agent:Row,drafts:Row[]):string|undefined {
 if(agent.account_id==null)return undefined;
 let latest:string|undefined,latestTime=-Infinity;
 for(const draft of drafts){
  if(draft.agent_id!==agent.id||draft.account_id!==agent.account_id||(draft.status||draft.state)!=='published'||!['post','reply','repost'].includes(draft.action)||draft.result?.already_applied||draft.relationship_already_applied||draft.result?.relationship_already_applied)continue;
  const publishedAt=timestamp(draft.published_at),publishedTime=publishedAt?Date.parse(publishedAt):NaN;
  if(Number.isFinite(publishedTime)&&publishedTime>latestTime){latest=publishedAt;latestTime=publishedTime;}
 }
 return latest;
}
function normalizeState(raw?:Row):AgentState {
 if(!raw)return emptyState;
 const rows=(value:unknown):Row[]=>Array.isArray(value)?value:typeof value==='object'&&value!==null?Object.values(value):[];
 const draftRows=rows(raw.drafts);
 return {...emptyState,...raw,settings:{...emptyState.settings,...raw.settings},agents:rows(raw.agents).map(agent=>({...agent,id:String(agent.id),interests:Array.isArray(agent.interests)?agent.interests.join('\n'):agent.interests||'',allowed_actions:agent.allowed_actions??legacyActions,freshness_minutes:agent.freshness_minutes??60,news_freshness_hours:agent.news_freshness_hours??24,notification_interval_seconds:agent.notification_interval_seconds??60,inbox:rows(agent.inbox),activity:rows(agent.activity),error:agent.error||agent.last_error||'',last_read:timestamp(agent.last_read_at??agent.last_read),last_run:timestamp(agent.last_run??agent.last_run_at),last_publish:timestamp(agent.last_publish)||timestamp(agent.last_publish_at)||lastPublishedAt(agent,draftRows),next_run:timestamp(agent.next_run??agent.next_run_at),status:agent.busy?'running':agent.busy===false&&['running','reading','generating','publishing'].includes(agent.status)?'idle':agent.status||'idle'} as Agent)),drafts:draftRows.map(draft=>({...draft,id:String(draft.id),agent_id:String(draft.agent_id),body:draft.body||'',target_post_id:draft.target_post_id??draft.reply_to,status:({pending:'draft',rejected:'failed'} as Record<string,string>)[draft.status||draft.state]||draft.status||draft.state||'draft',sources:rows(draft.sources)} as Draft)),events:rows(raw.events)};
}
function validationError(error:unknown){return !!error&&typeof error==='object'&&'errorFields' in error;}
function generationActive(agent?:Agent){return ['queued','running','cancelling'].includes(agent?.generation?.status||'');}
function leaseRecovery(agent:Agent){return agent.busy!==false&&agent.busy_reason==='lease_recovery';}
function agentRunning(agent:Agent,running?:unknown){return generationActive(agent)||(typeof agent.busy==='boolean'?agent.busy:['running','reading','generating','publishing'].includes(agent.status||'')||Array.isArray(running)&&running.includes(agent.id));}
function currentAgentError(agent:Agent){return generationActive(agent)||['failed','waiting'].includes(agent.generation?.status||'')?'':agent.error||'';}
function unfinishedDraft(draft:Draft,agent?:Agent){return !(draft.status==='skipped'&&draft.policy_blocked)&&(['draft','failed','uncertain','sending'].includes(draft.status)||(draft.action==='skip'&&draft.status==='skipped'&&!!draft.notification_ids?.length&&!draft.notifications_resolved&&!draft.notification_confirmation_authorized_at&&(agent?.mode==='review'||!!draft.requires_review)));}
function generationLabel(agent:Agent){return leaseRecovery(agent)?'等待旧任务释放':agent.generation?.stage==='publishing'?'正在执行已确认操作':agent.generation?.status==='queued'?'等待调度':agent.generation?.status==='cancelling'?'正在取消':'等待完整结果';}
function runningLabel(agent:Agent){return leaseRecovery(agent)?'等待旧任务释放':agent.busy_reason==='maintenance'?'维护中':generationActive(agent)?generationLabel(agent):agent.busy_reason==='operation'?'正在执行操作':agentLabels[agent.status||'running']||'正在运行';}
function GenerationProgress({agent}:{agent:Agent}){
 const job=agent.generation,failure=job?.status==='failed'?<Alert type="error" showIcon title="本轮决策失败" description={job.error||'本轮任务未完成，请查看个人日志后重试。'}/>:null;
 if(leaseRecovery(agent))return <><div className="agents-generation-progress" role="status"><strong>等待旧任务释放</strong><p>{agent.busy_until?`预计释放时间：${time(timestamp(agent.busy_until))}。`:'正在等待旧任务释放占用。'}释放后自动恢复操作。</p></div>{failure}</>;
 if(!job)return null;
 if(job.status==='waiting'){
  const publish=job.waiting_for==='publish';
  return <div className="agents-generation-progress" role="status"><strong>{publish?'内容已生成，等待执行':'等待决策机会'}</strong><p>{job.wait_message||(publish?'已保存本轮操作，等待可用执行机会。':'等待可用资源后继续本轮决策。')}</p><p>{job.retry_at?`下次重试时间：${time(timestamp(job.retry_at))}。`:'下次重试时间待调度确认。'}{!agent.enabled?'角色已暂停，恢复运行后再安排重试。':publish&&agent.mode==='review'?'当前为审阅模式，请到操作记录确认执行。':'后续执行以当前运行设置和可用额度为准。'}</p>{publish&&job.draft_id&&<Link to={`/ai/agents/${encodeURIComponent(agent.id)}?tab=drafts`}>查看待执行操作 ↗</Link>}</div>;
 }
 if(generationActive(agent)){
  const stage=job.status==='queued'?'任务已提交，等待开始。':job.status==='cancelling'?'正在结束本次任务。':({reading:'正在阅读近期消息。',observing:'正在阅读近期消息。',context:'正在结合角色记忆整理上下文。',dns:'正在解析 AI 服务地址。',connect:'正在连接 AI 服务。',tls:'正在建立安全连接。',send:'正在发送模型请求。',headers:'请求已发送，等待服务商响应。',body:'已建立响应，等待完整内容。',generating:'正在思考并组织回复。',thinking:'正在思考并组织回复。',receiving:'正在接收完整回答。',complete:'回答已接收，正在检查结果。',validating:'正在检查完整回复。',publishing:'生成已完成，正在执行已确认的操作，请到操作记录核对结果。'} as Record<string,string>)[job.stage||'']||'正在等待完整的思考结果。';
  return <div className="agents-generation-progress" role="status"><strong>{generationLabel(agent)}</strong><p>{stage} 已等待 {Math.max(0,Math.floor(job.elapsed_seconds||0))} 秒。</p><p>完成后自动更新。离开或刷新页面后，仍可回来查看进度。</p></div>;
 }
 if(failure)return failure;
 if(job.status==='cancelled')return <p className="agents-draft-note">本次阅读与决策已取消。</p>;
 return null;
}

const feedbackMetrics:[keyof FeedbackCounts,string][]=[['like_count','点赞'],['reply_count','回复'],['repost_count','转发'],['view_count','浏览']];
function feedbackValue(value:unknown){return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('zh-CN'):'待采集';}
export function AgentPerformance({agent}:{agent:Agent}){
 const accountId=agent.account_id;
 if(!accountId)return <section className="agents-performance" aria-label={`${agent.name}的账号与表现`}><h4>账号与表现</h4><p className="muted">待绑定账号，连接后可查看这个角色的帖子和互动反馈。</p></section>;
 const feedback=agent.feedback&&(!agent.feedback.account_id||agent.feedback.account_id===accountId)?agent.feedback:undefined;
 const ownPosts=(feedback?.recent_posts||[]).slice(0,3),actions=(feedback?.actions||[]).filter(action=>['published','completed','confirmed','succeeded','success'].includes(action.status)).slice(0,5);
 const official=feedback?.official,estimate=feedback?.score_estimate,postCount=feedback?.window?.post_count,snapshot=official?.team_snapshot;
 const analysisLink=`/analysis?author_id=${accountId}&analysis_tab=cohorts`,postsLink=`/posts?feed=all&author_id=${accountId}`;
 return <section className="agents-performance" aria-label={`${agent.name}的账号与表现`}><div className="agents-section-head"><h4>账号与表现</h4><Link to={`/accounts?q=${accountId}`}>账号 #{accountId} ↗</Link></div><Space size={[12,5]} wrap><Link to={postsLink}>账号帖子</Link><Link to={analysisLink}>账号分析</Link><Link to={`/analysis?author_id=${accountId}&analysis_tab=growth`}>窗口增长</Link></Space>
  {!feedback?<p className="muted">等待汇总这个账号的发布记录与互动样本。</p>:<><p className="agents-performance-summary">已采集账号帖子 {feedback.known_post_count==null?'待确认':`${feedback.known_post_count} 条`} · 已核实执行 {feedback.confirmed_action_count==null?'待确认':`${feedback.confirmed_action_count} 次`}</p>
   <div className="agents-feedback-metrics">{feedbackMetrics.map(([key,label])=><div key={key}><span>{label}</span><strong>{feedbackValue(feedback.metrics?.[key])}</strong><small>{feedback.metric_coverage?.[key]==null||postCount==null?'采样覆盖待确认':`覆盖 ${feedback.metric_coverage[key]}/${postCount} 帖`}</small></div>)}</div>
   <p className="agents-performance-caption">最近 {postCount??'待确认'} 条本人帖子中的已采集计数。最近反馈：{feedback.latest_sampled_at?time(timestamp(feedback.latest_sampled_at)):'尚未采到互动样本'}</p>
   <div className="agents-performance-score"><span><Tag>估算分</Tag>{estimate?.points_decimal!=null?estimate.points_decimal:'暂无法估算'}</span><span><Tag>角色官方积分</Tag>{official?.status==='attributed'&&official.points_decimal!=null?official.points_decimal:'暂无可归因记录'}</span><Link to="/score">查看本队官方积分与来源 ↗</Link></div>
   <Collapse size="small" className="agents-feedback-details" items={[{key:'evidence',label:'查看发布记录与反馈依据',children:<div className="agents-feedback-evidence"><p className="muted">{estimate?.reason||'公开互动计数不足以换算为比赛得分。'}</p>{official?.reason&&<p className="muted">{official.reason}</p>}{snapshot&&<p className="muted">全队官方快照：{snapshot.total_decimal??'待确认'} 分 · {time(timestamp(snapshot.observed_at))} · 来源：{({manual:'手动登记',automatic:'自动采集',portal:'赛事后台'} as Record<string,string>)[snapshot.source||'']||snapshot.source||'未记录'}。此总分属于全队。</p>}
    <div><h5>账号最近发布</h5>{!ownPosts.length?<p className="muted">尚未采到这个账号的帖子。</p>:ownPosts.map(post=><article key={post.post_id}><Space wrap><Tag>{actionLabels[post.action as ActionType]||'帖子'}</Tag><Link to={`/posts?post=${post.post_id}`}>原帖 #{post.post_id} ↗</Link>{post.remote_deleted_at&&<Tag>远端已删除</Tag>}<Link to={`/analysis?author_id=${accountId}&analysis_tab=deep&focus_post_id=${post.post_id}`}>分析这条帖子 ↗</Link></Space>{post.body&&<p className="agents-feedback-body">{post.body}</p>}<p className="muted">{feedbackMetrics.map(([key,label])=>`${label} ${feedbackValue(post.latest_counts?.[key])}`).join(' · ')}</p><p className="muted">{post.sampled_at?`采集于 ${time(timestamp(post.sampled_at))}`:'互动尚未采样'}{post.stale?' · 样本较早':''}</p></article>)}</div>
    <div><h5>已核实的操作</h5>{!actions.length?<p className="muted">还没有可核实的外部操作。</p>:actions.map(action=>{
     const publishedId=['post','reply','repost'].includes(action.action)?action.post_id:undefined,targetId=action.target_post_id||(['like','unlike'].includes(action.action)?action.performance_post_id||action.post_id:undefined);
     return <article key={action.draft_id}><Space wrap><Tag>{actionLabels[action.action as ActionType]||action.action}</Tag>{action.origin==='manual'&&<Tag>手动操作</Tag>}{action.remote_deleted_at&&<Tag>远端已删除</Tag>}{action.target_account_id&&<Link to={`/accounts?q=${action.target_account_id}`}>互动账号 #{action.target_account_id} ↗</Link>}{publishedId&&<Link to={`/posts?post=${publishedId}`}>发布记录 #{publishedId} ↗</Link>}{targetId&&targetId!==publishedId&&<Link to={`/posts?post=${targetId}`}>互动对象 #{targetId} ↗</Link>}<Link to={action.performance_scope==='own_post'&&publishedId?`/analysis?author_id=${accountId}&analysis_tab=deep&focus_post_id=${publishedId}`:analysisLink}>查看角色分析 ↗</Link></Space>{action.performance_scope==='deletion'?<p className="muted">远端删除已核对，原记录保留用于审计。</p>:action.performance_scope==='target_context'?<p className="muted">查看互动对象的反馈，不计入这个角色的发布表现。</p>:action.performance_scope==='own_post'&&action.performance?<p className="muted">该操作帖子的反馈：{feedbackMetrics.map(([key,label])=>`${label} ${feedbackValue(action.performance?.latest_counts?.[key])}`).join(' · ')}</p>:<p className="muted">该操作暂未形成可采集的反馈。</p>}</article>;
    })}</div>
   </div>}]} />
  </>}
 </section>;
}

const completedStates=new Set(['published','completed','succeeded','success']);
const pendingStates=new Set(['pending','draft','sending','failed','rejected']);
function memoryState(entry:MemoryEntry){return entry.state||entry.status||'';}
function memoryRows(value:unknown):MemoryEntry[]{return Array.isArray(value)?value.filter(row=>!!row&&typeof row==='object'):[];}
function subjectiveKind(entry:MemoryEntry){
 if(['viewpoints','questions','people'].includes(entry.kind||''))return entry.kind;
 const id=String(entry.id||'');
 if(id.startsWith('viewpoints:')||entry.title==='主观看法')return 'viewpoints';
 if(id.startsWith('questions:')||entry.title?.startsWith('仍感兴趣的疑问'))return 'questions';
 if(entry.title==='对人的主观印象')return 'people';
 return 'thoughts';
}
function memoryLabel(entry:MemoryEntry,section:string){
 if(entry.title)return entry.title;
 const action=actionLabels[entry.action as ActionType]||entry.action;
 if(section==='reading')return entry.source_id?.startsWith('news:')?'读到一则新闻':`读到帖子${entry.post_id?` #${entry.post_id}`:''}`;
 if(section==='relationships')return `与账号 ${entry.author_id??'未知'} 的往来`;
 if(section==='thoughts')return '当时的想法';
 if(section==='viewpoints')return '主观看法';
 if(section==='questions')return '仍想了解的问题';
 if(section==='people')return entry.author_id?`对账号 ${entry.author_id} 的印象`:'对人的印象';
 return action||'一次互动';
}
function memoryStatus(entry:MemoryEntry,section:string){
 if(section==='completed'&&entry.already_applied)return '仅核对已有状态';
 const value=memoryState(entry);
 if(value==='uncertain'||section==='uncertain')return '结果待核对';
 if(value==='failed'||value==='rejected')return '执行失败';
 if(value==='sending')return '正在执行';
 if(value==='discarded')return '已放弃';
 if(value==='skipped')return '决定跳过';
 if(section==='pending')return '待审阅，尚未执行';
 if(section==='completed')return '已完成';
 return '';
}
export function ExperienceMemory({agent}:{agent:Agent}){
 const [historyOpen,setHistoryOpen]=useState(false);
 const view=agent.memory_view;
 const oldActivity=memoryRows(agent.activity);
 const reading:MemoryEntry[]=view?.observed_reading!==undefined?memoryRows(view.observed_reading):(agent.last_sources||[]).map(source=>({id:source.id,source_id:source.id,title:source.title,excerpt:source.text||source.body,post_id:source.post_id,at:source.observed_at||source.first_seen,origin:source.origin||source.kind}));
 const candidates=view?.completed_actions!==undefined?memoryRows(view.completed_actions):oldActivity.filter(entry=>completedStates.has(memoryState(entry)));
 const completed=candidates.filter(entry=>!memoryState(entry)||completedStates.has(memoryState(entry)));
 const pending=[...memoryRows(view?.pending_intentions),...candidates.filter(entry=>pendingStates.has(memoryState(entry)))];
 if(!view)pending.push(...oldActivity.filter(entry=>pendingStates.has(memoryState(entry))));
 const uncertain=[...memoryRows(view?.uncertain_actions),...candidates.filter(entry=>memoryState(entry)==='uncertain')];
 if(!view)uncertain.push(...oldActivity.filter(entry=>memoryState(entry)==='uncertain'));
 const notes:MemoryEntry[]=typeof view?.subjective_notes==='string'?[{excerpt:view.subjective_notes}]:memoryRows(view?.subjective_notes);
 const groups=[{key:'viewpoints',label:'长期观点',entries:notes.filter(note=>subjectiveKind(note)==='viewpoints'),empty:'还没有形成需要长期保留的观点。'},{key:'questions',label:'持续关注的问题',entries:notes.filter(note=>subjectiveKind(note)==='questions'),empty:'目前没有持续关注的问题。'},{key:'people',label:'对人的印象',entries:notes.filter(note=>subjectiveKind(note)==='people'),empty:'还没有记录对具体人物的印象。'},{key:'reading',label:'读过的内容',entries:reading,empty:'还没有留下阅读记录。'},{key:'completed',label:'实际做过的事',entries:completed,empty:'还没有核实完成的互动。'},{key:'pending',label:'还没完成的打算',entries:pending,empty:'目前没有待办的互动。'},{key:'uncertain',label:'结果还没确认的操作',entries:uncertain,empty:'目前没有需要核对的操作。'},{key:'relationships',label:'交流记录与关系线索',entries:memoryRows(view?.relationships),empty:'还没有积累与其他账号的往来。'},{key:'thoughts',label:'当时的想法',entries:notes.filter(note=>subjectiveKind(note)==='thoughts'),empty:'角色会在阅读与思考后留下自己的理解。'}];
 return <div className="agents-experience"><div className="agents-section-head agents-memory-heading"><div><h4>长期记忆与回忆</h4><p className="muted agents-memory-intro">观点、疑问和人物印象会随阅读与交流积累，下一次判断会结合这些记忆。阅读经历、已完成的互动和待办分别保存。</p>{view?.long_term&&<p className="muted">已保存 {view.long_term.total??0} 条回忆{view.long_term.latest_at?`，最近更新 ${time(view.long_term.latest_at)}`:''}。近期内容显示在下方，较早的内容可通过历史回忆查找。</p>}</div><Button onClick={()=>setHistoryOpen(true)}>查找历史回忆</Button></div><Collapse size="small" defaultActiveKey={['viewpoints','questions','people','reading','completed']} items={groups.map(group=>({key:group.key,label:`${group.label} · ${group.entries.length} 条`,children:<div className="agents-memory-list">{['viewpoints','questions','people','thoughts'].includes(group.key)&&!!group.entries.length&&<p className="muted">角色当时的理解，会随新资料修正。</p>}{!group.entries.length?<p className="muted">{group.empty}</p>:group.entries.slice(0,8).map((entry,index)=>{
  const status=memoryStatus(entry,group.key),postId=entry.post_id||(group.key==='reading'&&entry.source_id?.startsWith('post:')?Number(entry.source_id.slice(5)):undefined),targetId=entry.target_post_id;
  return <article key={`${entry.id||entry.draft_id||entry.source_id||index}-${index}`}><div className="agents-section-head"><strong>{memoryLabel(entry,group.key)}</strong><Space size={5} wrap>{status&&<Tag color={status==='已完成'?'green':status==='执行失败'?'red':status==='结果待核对'?'orange':'default'}>{status}</Tag>}{(entry.at||entry.created_at||entry.last_read_at)&&<span className="muted">{time(timestamp(entry.at||entry.created_at||entry.last_read_at))}</span>}</Space></div>{(entry.excerpt||entry.body)&&<p className="agents-prose">{entry.excerpt||entry.body}</p>}{entry.already_applied&&<p className="agents-memory-reason">远端原已存在此状态，本次只做核对，没有新增互动。</p>}{entry.reason&&entry.reason!==(entry.excerpt||entry.body)&&<p className="agents-memory-reason">当时的考虑：{entry.reason}</p>}<Space size={[12,5]} wrap>{!!postId&&<Link to={`/posts?post=${postId}`}>{group.key==='reading'?'回看原帖':group.key==='completed'&&['post','reply','repost'].includes(entry.action||'')?'查看已发布帖子':'查看关联帖子'} #{postId} ↗</Link>}{!!entry.target_account_id&&<Link to={`/accounts?q=${entry.target_account_id}`}>互动账号 #{entry.target_account_id} ↗</Link>}{!!targetId&&targetId!==postId&&<Link to={`/posts?post=${targetId}`}>互动目标 #{targetId} ↗</Link>}{!postId&&entry.source_id?.startsWith('news:')&&entry.title&&<Link to={`/news?q=${encodeURIComponent(entry.title)}`}>回看新闻 ↗</Link>}{entry.author_id&&['relationships','people'].includes(group.key)&&<Link to={`/accounts?q=${entry.author_id}`}>查看账号 #{entry.author_id} ↗</Link>}{(entry.read_count||0)>1&&<span className="muted">读过 {entry.read_count} 次</span>}</Space>{['viewpoints','questions','people'].includes(group.key)&&<MemorySources ids={entry.source_ids} sources={entry.evidence}/>}</article>;
 })}{group.entries.length>8&&<p className="muted">此处展示最近 8 条，共 {group.entries.length} 条。</p>}</div>}))}/><Modal title={`${agent.name} · 历史回忆`} open={historyOpen} footer={null} width={820} onCancel={()=>setHistoryOpen(false)} destroyOnHidden>{historyOpen&&<AgentMemoryHistory key={agent.id} agentId={agent.id} agentName={agent.name}/>}</Modal></div>;
}

// The team snapshot leaves out memory views (2.3 MB for 15 roles); load the
// shown role's own. Writes invalidate it (see perform).
function RoleMemory({agent}:{agent:Agent}){
 const path=`/agents/${encodeURIComponent(agent.id)}/memory`;
 const query=useQuery<Row,Error>({queryKey:[path,{}],queryFn:context=>request<Row>(path,{signal:context.signal}),enabled:!agent.memory_view,refetchInterval:30000,refetchOnWindowFocus:true});
 if(agent.memory_view)return <ExperienceMemory agent={agent}/>;
 if(query.isPending)return <Skeleton active paragraph={{rows:4}}/>;
 if(query.isError)return <Alert type="error" showIcon title="记忆读取失败" description={query.error.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>;
 const loaded=query.data;
 return <ExperienceMemory agent={{...agent,...(loaded.memory_view?{memory_view:loaded.memory_view as MemoryView}:{}),...(Array.isArray(loaded.last_sources)?{last_sources:loaded.last_sources}:{}),...(Array.isArray(loaded.activity)?{activity:loaded.activity}:{})}}/>;
}

function DraftSources({sources}:{sources:Source[]}){
 return <div className="agents-sources">{sources.map((source,index)=><article key={`${source.id||index}`}><div className="agents-section-head"><Space wrap><Tag>{sourceLabels[source.origin||source.kind||'']||(source.kind==='news'?'新闻':'帖子')}</Tag>{source.context_only&&<Tag>父帖或根帖背景</Tag>}<strong>{source.title||source.site||`资料 ${index+1}`}</strong></Space><Space wrap>{source.post_id&&<Link to={`/posts?post=${source.post_id}`}>原帖 #{source.post_id} ↗</Link>}{source.url&&/^https?:\/\//i.test(source.url)&&<a href={source.url} target="_blank" rel="noreferrer">来源 ↗</a>}</Space></div>{(source.text||source.body)&&<p className="agents-prose">{source.text||source.body}</p>}{!!source.thread_context?.missing?.length&&<p className="agents-draft-note">本条资料尚缺 {source.thread_context.missing.length} 条父帖或根帖上下文。</p>}<div className="agents-source-meta">{source.created_at&&<span>发帖时间 {time(timestamp(source.created_at))}</span>}{source.published_at&&<span>新闻剧情时间 {time(timestamp(source.published_at))}</span>}{(source.observed_at||source.first_seen)&&<span>采集时间 {time(timestamp(source.observed_at||source.first_seen))}</span>}{!!source.notification_ids?.length&&<span>通知 {source.notification_ids.map(id=>`#${id}`).join('、')}</span>}</div></article>)}</div>;
}

// The team snapshot leaves out a finished draft's sources; load them when opened.
function OmittedDraftSources({draftId}:{draftId:string}){
 const query=useApi<{draft?:Row}>(`/agents/drafts/${encodeURIComponent(draftId)}`,{},false);
 if(query.isPending)return <Skeleton active paragraph={{rows:2}}/>;
 if(query.isError)return <p className="agents-inline-error">{query.error.message}</p>;
 const sources=query.data?.draft?.sources;
 return Array.isArray(sources)&&sources.length?<DraftSources sources={sources}/>:<p className="muted">没有可显示的阅读来源。</p>;
}

function observedCount(value:unknown){return typeof value==='number'&&Number.isFinite(value)&&value>=0?value:undefined;}
export function ObservationSummary({agent}:{agent:Agent}){
 const observation=agent.observation;
 if(!observation||!Object.keys(observation).length)return <p className="muted">尚未观察近期消息与通知</p>;
 const selection=observation.reading_selection,thread=observation.thread_context;
 const candidateCount=observedCount(selection?.candidate_count),selectedCount=observedCount(selection?.selected_count),sourceCount=observedCount(observation.source_count);
 const included=observedCount(thread?.included_count),background=observedCount(thread?.background_count),missing=observedCount(thread?.missing_count),deferred=observedCount(thread?.deferred_count);
 const selectionError=typeof selection?.error==='string'?selection.error:undefined;
 const noCandidates=selection?.status==='empty'&&candidateCount===0;
 const selectionLabel=selection?.status==='empty'?(noCandidates?'本轮暂无候选资料':'AI 本轮未选中资料'):selection?.status==='fallback'?(selection.error?'筛选失败，已回退':'已使用回退资料'):selection?.status==='selected'?'AI 已筛选资料':'资料筛选记录';
 const threadDetails=[included!==undefined?`纳入目标 ${included} 条`:null,background!==undefined?`补充背景 ${background} 条`:null,missing!==undefined?`缺失上下文 ${missing} 处`:null,deferred!==undefined?`延后目标 ${deferred} 条`:null].filter(Boolean).join(' · ');
 return <div className="agents-observation"><div className="agents-section-head"><strong>最近观察</strong><span>{time(timestamp(observation.observed_at||observation.last_refresh_at))}</span></div><Space wrap><Tag>通知 · {observationLabels[observation.notifications_status||'']||'尚未检查'}</Tag><Tag>首页 · {observationLabels[observation.home_status||'']||'尚未检查'}</Tag>{!selection&&<span>阅读资料 {observation.source_count??0} 条 · 本轮新增 {observation.new_source_count??0} 条</span>}</Space>
  {selection&&<div aria-label="资料筛选记录"><Space wrap><Tag color={selection.status==='fallback'?'orange':selection.status==='selected'?'green':'default'}>{selectionLabel}</Tag>{selection.cache_hit&&<Tag>复用筛选结果</Tag>}{candidateCount!==undefined&&<span>预览候选 {candidateCount} 条</span>}{selectedCount!==undefined&&<span>{selection.status==='fallback'?'回退选用':'筛选选中'} {selectedCount} 条</span>}{sourceCount!==undefined&&<span>深读来源 {sourceCount} 条</span>}</Space>{selection.status==='empty'&&<p className="muted">{noCandidates?'本轮没有可供深读的候选资料。':'本轮未选中深读资料。'}</p>}{selection.reason&&<p className="muted">筛选说明：{selection.reason}</p>}{selectionError&&<p className="agents-inline-error">{selectionError}</p>}</div>}
  {thread&&threadDetails&&<p className="muted" aria-label="对话上下文记录">阅读打包：{threadDetails}</p>}
  <div className="agents-source-meta"><span>最近帖子 {time(timestamp(observation.latest_post_at))}</span><span>最近新闻采集 {time(timestamp(observation.latest_news_at))}</span>{observation.next_refresh_at&&<span>下次检查 {time(timestamp(observation.next_refresh_at))}</span>}</div>{observation.error&&<p className="agents-inline-error">{observation.error}</p>}</div>;
}

// Manual queue (F, D26): a member-confirmed post waits for the next shared slot and is sent once.
function queueText(queue:Row):string {
 const attempts=Number(queue.attempts)||0;
 if(queue.state==='waiting')return `排队中：有发布名额时自动发送一次 · 已尝试 ${attempts} 次 · ${time(timestamp(queue.expires_at))} 前没发出就停止排队`;
 if(queue.state==='sent')return `排队已发送${queue.post_id?` · 帖子 #${queue.post_id}`:''}`;
 if(queue.state==='expired')return '排队超时，没有发送，草稿保留为待确认';
 if(queue.state==='cancelled')return String(queue.reason||'已取消排队，草稿保留为待确认');
 return `排队已停止${queue.reason?`：${queue.reason}`:''}`;
}

export function DraftCard({draft,agent,busy,onPublish,onDiscard,onReconcile,onConfirmUnexecuted,onQueue,onCancelQueue}:{draft:Draft;agent?:Agent;busy:boolean;onPublish:(id:string,body:string,confirmationPostId?:number)=>Promise<boolean>;onDiscard:(id:string)=>Promise<boolean>;onReconcile:(id:string,postId?:number)=>Promise<boolean>;onConfirmUnexecuted?:(id:string,reason:string,version:number)=>Promise<boolean>;onQueue?:(id:string,body:string)=>Promise<boolean>;onCancelQueue?:(id:string)=>Promise<boolean>}){
 const [body,setBody]=useState(draft.body),[postId,setPostId]=useState<number|null>(null),[confirmationPostId,setConfirmationPostId]=useState<number|null>(null);
 const [discardVersion,setDiscardVersion]=useState<number|null>(null),[discardReason,setDiscardReason]=useState(''),[confirmedUnexecuted,setConfirmedUnexecuted]=useState(false);
 useEffect(()=>setBody(draft.body),[draft.id,draft.body]);
 const editable=['draft','failed'].includes(draft.status),uncertain=draft.status==='uncertain',requiresBody=['post','reply'].includes(draft.action),requiresAccount=draft.action!=='skip',relationshipAction=['like','unlike','follow','unfollow','repost','delete_post'].includes(draft.action),actionLabel=actionLabels[draft.action]||draft.action;
 const deletion=draft.action==='delete_post',writtenAction=['post','reply','repost'].includes(draft.action),styleWarnings=Array.isArray(draft.style_warnings)?draft.style_warnings.filter(warning=>typeof warning==='string'&&warning.trim()):[],bodyChanged=body!==draft.body;
 const reviewInvalidated=bodyChanged||draft.review?.status==='stale';
 const manual=draft.origin==='manual'||draft.manual_only===true||draft.manual_prepared===true,queue=draft.manual_queue,waiting=queue?.state==='waiting',queueable=manual&&draft.status==='draft'&&writtenAction&&!!onQueue;
 const resultText=typeof draft.result==='string'?draft.result:typeof draft.result==='object'&&draft.result!==null?String((draft.result as Row).message||(draft.result as Row).status||''):'';
 const policyBlocked=draft.status==='skipped'&&!!draft.policy_blocked,policyReason=draft.policy_reason||draft.last_error||resultText||'禁止 Agent 内部互动策略已拦截此操作。';
 return <article className={`agents-draft ${uncertain?'agents-draft-uncertain':''}`}>
  <div className="agents-section-head"><div><Space wrap><strong>{agent?.name||'未命名角色'}</strong><Tag color={policyBlocked?'orange':draft.status==='published'||draft.status==='skipped'?'green':uncertain?'orange':draft.status==='failed'?'red':'default'}>{policyBlocked?'策略已拦截':draftLabels[draft.status]||draft.status}</Tag><Tag>{actionLabel}</Tag>{(draft.manual||draft.origin==='manual'||draft.manual_only)&&<Tag>手动操作</Tag>}{draft.remote_deleted_at&&<Tag>远端已删除</Tag>}{draft.target_account_id&&<Link to={`/accounts?q=${draft.target_account_id}`}>目标账号 #{draft.target_account_id} ↗</Link>}{draft.target_post_id&&<Link to={`/posts?post=${draft.target_post_id}`}>目标帖子 #{draft.target_post_id} ↗</Link>}</Space><p className="muted">{time(timestamp(draft.created_at))}</p></div>{draft.post_id&&['post','reply','repost'].includes(draft.action)&&<Link to={`/posts?post=${draft.post_id}`}>查看帖子 #{draft.post_id} ↗</Link>}</div>
  {editable&&writtenAction?<Input.TextArea aria-label={`${agent?.name||'角色'}的草稿正文`} placeholder={draft.action==='repost'?'可选：为这次转发补充自己的看法':''} value={body} onChange={event=>setBody(event.target.value)} disabled={busy||waiting} autoSize={{minRows:draft.action==='repost'?2:4,maxRows:16}} maxLength={500} showCount/>:draft.body?<p className="agents-prose agents-draft-body">{draft.body}</p>:<p className="agents-draft-note">{draft.action==='delete_post'?'删除此角色本人发布的目标帖子。此操作不可恢复。':draft.action==='like'?'点赞此目标帖子，无需正文。':draft.action==='unlike'?'取消此目标帖子的点赞，无需正文。':draft.action==='follow'?'关注此目标账号，无需正文。':draft.action==='unfollow'?'取消关注此目标账号，无需正文。':draft.action==='skip'?'本轮不执行外部互动。':'直接转发目标帖子。'}</p>}
  {draft.manual_preview?.target_post&&<div className="agents-manual-inline-target"><p className="agents-draft-note">目标作者 #{draft.manual_preview.target_post.author_id}</p><p className="agents-prose">{draft.manual_preview.target_post.body||'此帖子没有文字正文。'}</p></div>}{editable&&deletion&&<div className="agents-manual-delete"><strong>删除后无法恢复。</strong><p>输入目标帖子 ID {draft.target_post_id} 确认删除本人帖子。</p><InputNumber aria-label="确认删除帖子 ID" min={1} precision={0} value={confirmationPostId} disabled={busy} onChange={value=>setConfirmationPostId(typeof value==='number'?value:null)}/></div>}{editable&&writtenAction&&<p className="agents-draft-note">可以按角色平时的说话方式调整表达。</p>}
  {editable&&writtenAction&&!!styleWarnings.length&&<aside className="agents-style-warning" aria-label="表达风格提示"><strong>{bodyChanged?'原稿的表达提示':'表达风格提示'}</strong><ul>{styleWarnings.map((warning,index)=><li key={`${index}-${warning}`}>{warning}</li>)}</ul><p>{bodyChanged?'正文已修改，可按当前内容确认执行。':'可按需要调整，确认后可执行。'}</p></aside>}
  {draft.reason&&<p className="agents-draft-note"><strong>操作理由：</strong>{draft.reason}</p>}
  {(writtenAction||draft.review)&&<aside aria-label="草稿发布前审核"><Space wrap><Tag>语言：{reviewInvalidated?'待重新检查':draft.review?reviewLanguageLabel(draft.review.language):'待检查 · English only'}</Tag><Tag>条款：{reviewInvalidated?'原审核已失效':draft.review?reviewClauseLabel(draft.review):'待执行前检查'}</Tag><span className="muted">执行：{draftLabels[draft.status]||draft.status}</span><Link to="/ai/review">查看 AI 审核 ↗</Link></Space>{reviewInvalidated?<p className="agents-draft-note">{bodyChanged?'正文已修改':'审核条件已改变'}，旧审核结果失效，提交执行前会重新检查。表情可以保留。</p>:draft.review_summary&&<p className="agents-draft-note">{draft.review_summary}</p>}</aside>}
  {!policyBlocked&&draft.requires_review&&<p className="agents-inline-error">{draft.review_hold_reason||draft.review_reason||'此操作已暂停自动执行，请审阅后手动确认。'}</p>}
  {policyBlocked?<p className="agents-draft-note"><strong>拦截原因：</strong>{policyReason}</p>:draft.last_error&&<p className="agents-inline-error">{draft.last_error}</p>}
  {(resultText||draft.status==='published'||draft.status==='skipped')&&<p className="agents-action-result">操作结果：{policyBlocked?'策略已拦截':resultText||(draft.action==='skip'?'已记录跳过':`${actionLabel}已完成`)}</p>}
  {!!draft.notification_ids?.length&&<p className="agents-draft-note">关联通知：{draft.notification_ids.map(id=>`#${id}`).join('、')}</p>}
  {!policyBlocked&&draft.action==='skip'&&draft.status==='skipped'&&!!draft.notification_ids?.length&&!draft.notifications_resolved&&!draft.notification_confirmation_authorized_at&&<p className="agents-draft-note"><Link to={`/ai/agents/${draft.agent_id}?tab=inbox`}>前往确认关联通知</Link></p>}
  {!!draft.source_ids?.length&&!draft.sources?.length&&!draft.sources_omitted&&<p className="agents-draft-note">依据资料：{draft.source_ids.join('、')}</p>}
  {uncertain&&<div className="agents-reconcile"><p>{relationshipAction?'检查此角色账号的远端记录，确认该目标上的操作是否成功。':'执行结果尚未确认，请找到对应帖子后填写帖子 ID 完成核对。'}</p><Space wrap>{!relationshipAction&&<InputNumber aria-label="核对帖子 ID" placeholder="帖子 ID" min={1} precision={0} value={postId} disabled={busy} onChange={value=>setPostId(typeof value==='number'?value:null)}/>}<Button disabled={busy||!relationshipAction&&!postId} onClick={()=>void onReconcile(draft.id,postId||undefined)}>{relationshipAction?'核对远端操作':'确认已发布'}</Button>{onConfirmUnexecuted&&<Button disabled={busy} onClick={()=>{setDiscardReason('');setConfirmedUnexecuted(false);setDiscardVersion(draft.version||1);}}>人工确认未执行</Button>}</Space></div>}
  <Modal title="确认操作未执行并放弃" open={uncertain&&discardVersion!==null} okText="确认未执行并放弃" cancelText="返回核对" confirmLoading={busy} closable={!busy} maskClosable={false} onCancel={()=>{if(!busy)setDiscardVersion(null);}} okButtonProps={{danger:true,disabled:busy||!confirmedUnexecuted||discardReason.trim().length<3}} onOk={async()=>{if(onConfirmUnexecuted&&discardVersion!==null&&confirmedUnexecuted&&discardReason.trim().length>=3&&await onConfirmUnexecuted(draft.id,discardReason.trim(),discardVersion))setDiscardVersion(null);}}>
   <p>{agent?.name||'此角色'} · 账号 #{draft.account_id??agent?.account_id??'未知'} · {actionLabel}{draft.target_post_id?` · 目标帖子 #${draft.target_post_id}`:draft.target_account_id?` · 目标账号 #${draft.target_account_id}`:''}</p>
   <p>请先在平台核实这项操作未发生。放弃后，该角色可以继续操作；此操作仅关闭本地记录，不能撤销已经发出的请求。</p>
   <Input.TextArea aria-label="未执行确认依据" placeholder="说明检查了哪些远端记录、为什么确认操作未发生（至少 3 个字）" value={discardReason} maxLength={1000} disabled={busy} autoSize={{minRows:3,maxRows:8}} onChange={event=>setDiscardReason(event.target.value)}/>
   <Checkbox checked={confirmedUnexecuted} disabled={busy} onChange={event=>setConfirmedUnexecuted(event.target.checked)}>我已检查远端记录，确认这项操作未执行</Checkbox>
  </Modal>
  {!!draft.sources?.length&&<Collapse size="small" className="agents-source-collapse" items={[{key:'sources',label:`阅读来源 · ${draft.sources.length} 条`,children:<DraftSources sources={draft.sources}/>}]}/>}
  {!draft.sources?.length&&!!draft.sources_omitted&&<Collapse size="small" className="agents-source-collapse" items={[{key:'sources',label:`阅读来源 · ${draft.sources_omitted} 条`,children:<OmittedDraftSources draftId={draft.id}/>}]}/>}
  {manual&&queue?.state&&<p className={waiting?'agents-draft-note':'agents-action-result'} aria-label="排队状态">{queueText(queue)}</p>}
  {editable&&<div className="agents-draft-actions"><span className="muted">{waiting?'排队中可以取消排队；放弃操作也会结束排队':requiresAccount&&!agent?.account_id?'连接此角色的账号后即可执行':requiresBody?'审阅正文与依据后执行':'审阅目标与理由后执行'}</span><Space><Button disabled={busy} onClick={()=>void onDiscard(draft.id)}>放弃操作</Button>{waiting?<Button disabled={busy} onClick={()=>void onCancelQueue?.(draft.id)}>取消排队</Button>:<>{queueable&&<Tooltip title={bodyChanged?'排队只发送预览过的正文；改了正文请直接发布，或放弃后重新准备':'全队发布名额满时，排到下一个名额自动发送，只发一次；15 分钟内没发出就停止排队'}><Button disabled={busy||bodyChanged||!agent?.account_id} onClick={()=>void onQueue?.(draft.id,draft.body)}>排队发布</Button></Tooltip>}<Button type="primary" danger={deletion} disabled={busy||requiresBody&&!body.trim()||body.length>500||requiresAccount&&!agent?.account_id||deletion&&(confirmationPostId!==draft.target_post_id||draft.manual_preview?.target_post?.author_id!==agent?.account_id)} onClick={()=>void (deletion?onPublish(draft.id,'',confirmationPostId||undefined):onPublish(draft.id,writtenAction?body:''))}>{draft.action==='post'?'发布':draft.action==='skip'?'记录跳过':`确认${actionLabel}`}</Button></>}</Space></div>}
 </article>;
}

function AgentWorkspace({agent,state,busy,running,providerReady,tab,onTab,onToggle,onRead,onRun,onCancel,onEditor,onAccount,drafts,inbox}:{agent:Agent;state:AgentState;busy:boolean;running:boolean;providerReady:boolean;tab:string;onTab:(value:string)=>void;onToggle:()=>void;onRead:()=>void;onRun:()=>void;onCancel:()=>void;onEditor:()=>void;onAccount:()=>void;drafts:ReactNode;inbox:ReactNode}){
 const inboxCount=(agent.inbox||[]).filter(notificationUnfinished).length;
 const roleDrafts=state.drafts.filter(item=>item.agent_id===agent.id),pending=roleDrafts.filter(item=>unfinishedDraft(item,agent));
 const ownAccounts=useOwnAccounts(),risk=ownAccounts.risk(agent.account_id),identity=ownAccounts.identity(agent.account_id);
 const config=<section className="agents-detail-panel stack"><div className="agents-section-head"><div><h3>身份、人设与运行配置</h3><p className="muted">每次阅读和决策会结合这些设定及本人的经历。</p></div><Button disabled={busy} onClick={onEditor}>编辑完整配置</Button></div><div className="agents-config-sections">{[['身份与历史背景',agent.background],['个人立场',agent.stance],['表达习惯',agent.voice],['关注的话题',agent.interests]].map(([label,value])=><section key={label}><h4>{label}</h4><p className="agents-prose">{value||'尚未设置'}</p></section>)}</div><dl className="agents-detail-facts"><div><dt>主要语言</dt><dd>English only · 允许表情</dd></div><div><dt>操作方式</dt><dd>{agent.mode==='auto'?'决策后自动执行':'生成操作，审阅后执行'}</dd></div><div><dt>无新消息时检查</dt><dd>{Number(((agent.interval_seconds||1800)/60).toFixed(1))} 分钟</dd></div><div><dt>帖子新鲜度</dt><dd>{agent.freshness_minutes} 分钟</dd></div><div><dt>新闻新鲜度</dt><dd>{agent.news_freshness_hours} 小时</dd></div><div><dt>通知检查</dt><dd>{agent.notification_interval_seconds} 秒</dd></div></dl><div><h4>允许自主选择的动作</h4><Space wrap>{(agent.allowed_actions||legacyActions).map(action=><Tag key={action}>{actionLabels[action]}</Tag>)}</Space></div><p className="muted">{state.settings.block_agent_interactions?"已禁止受管 Agent 之间回复、点赞、关注、转发及正文 @提及；内部账号的取消点赞、取消关注仅限人工清理。外部互动仍由角色自主决定。":"关注、取消关注和取消点赞由角色自主决定，执行遵守赛事平台额度并核对结果。"}可在完整配置中调整允许的动作。</p><Link to="/ai/settings">管理 AI 服务配置 ↗</Link></section>;
 const overview=<div className="stack">{agent.account_suspended&&<Alert type="error" showIcon title="平台已封禁这个账号，不能再登录或发帖" description={`${time(agent.account_suspended.at)} · ${agent.account_suspended.message||''}`}/>}<RiskAlert risk={risk}/><section className="agents-detail-panel stack"><div className="agents-section-head"><div><h3>运行与健康状态</h3><p className="muted">{running?runningLabel(agent):agent.enabled?(state.settings.enabled?'等待统一调度':'角色已启用，队伍持续运行已暂停'):'角色已暂停'}</p></div><Button disabled={busy} onClick={onToggle}>{agent.enabled?'暂停此角色':'启用此角色'}</Button></div><div className="agents-detail-stats"><div><span>已连接账号</span><strong>{agent.account_id?`#${agent.account_id}`:'未连接'}</strong></div><div><span>未完成操作</span><strong>{pending.length}</strong></div><div><span>平台未确认通知</span><strong>{inboxCount}</strong></div><div><span>无新消息时检查</span><strong>{Number(((agent.interval_seconds||1800)/60).toFixed(1))} 分钟</strong></div></div>{currentAgentError(agent)?<Alert type="error" showIcon title="最近错误" description={currentAgentError(agent)}/>:!generationActive(agent)&&agent.generation?.status!=="failed"&&<p className="muted">当前没有未清除的运行错误。</p>}<GenerationProgress agent={agent}/>{!providerReady&&<Alert type="info" showIcon title="文字服务尚未就绪" description={<span>可先只阅读。在 <Link to="/ai/settings">AI 配置</Link> 中启用文字服务后，可以生成下一步操作。</span>}/>}<Space wrap><Button disabled={busy||running} onClick={onRead}>只阅读</Button><Button type="primary" loading={generationActive(agent)&&!leaseRecovery(agent)} disabled={busy||running||!providerReady} onClick={onRun}>阅读并决策</Button>{generationActive(agent)&&!leaseRecovery(agent)&&agent.generation?.stage!=='publishing'&&<Button disabled={busy||agent.generation?.status==='cancelling'} onClick={onCancel}>{agent.generation?.status==='cancelling'?'正在取消':'取消本次决策'}</Button>}<Button disabled={busy||!state.sdk_available} onClick={onAccount}>{agent.account_id?'账号设置':'连接账号'}</Button></Space><dl className="agents-detail-facts"><div><dt>上次阅读</dt><dd>{time(agent.last_read)}</dd></div><div><dt>上次决策</dt><dd>{time(agent.last_run)}</dd></div><div><dt>最近发布</dt><dd>{time(agent.last_publish)}</dd></div><div><dt>空闲检查</dt><dd>{agent.enabled&&state.settings.enabled?time(agent.next_run):'已暂停'}</dd></div></dl></section>{!!agent.account_id&&ownAccounts.loaded&&<div className="agents-detail-panel"><IdentityDetail row={identity} accountId={agent.account_id}/></div>}<ObservationSummary agent={agent}/><AgentPerformance agent={agent}/>{tab==='overview'&&<fieldset disabled={!!state.maintenance?.active} style={{border:0,minWidth:0,padding:0,margin:0}}><AgentResearchReference key={agent.id} agentId={agent.id}/></fieldset>}</div>;
 const memory=<section className="agents-detail-panel stack"><div className="agents-section-head"><div><h3>经历与记忆</h3><p className="muted">已读内容、真实操作和未完成打算分别保存。</p></div><Button disabled={busy} onClick={onEditor}>编辑或清空记忆</Button></div><section><h4>手动补充的长期笔记</h4><p className="agents-prose">{agent.memory||'尚未添加手动笔记，角色自动积累的记忆见下方。'}</p></section><RoleMemory agent={agent}/></section>;
 return <div className="agents-detail stack"><div className="agents-detail-heading"><div className="agents-role-title"><span className="agents-avatar" aria-hidden="true">{agent.name.slice(0,1)}</span><div><h3>{agent.name}</h3><span className="muted">@{agent.username}</span></div></div><Space wrap><Tag>{agent.mode==='auto'?'自动执行':'审阅后执行'}</Tag><Tag color={agent.enabled?'green':'default'}>{agent.enabled?'已启用':'已暂停'}</Tag>{agent.account_suspended&&<Tag color="red">已封禁</Tag>}<RiskTag risk={risk}/><IdentityTag row={identity}/>{running&&<Tag color={leaseRecovery(agent)?"orange":"processing"}>{runningLabel(agent)}</Tag>}<Button disabled={busy} onClick={onEditor}>编辑角色</Button></Space></div><Tabs activeKey={tab} onChange={onTab} items={[{key:'overview',label:'总览',children:overview},{key:'persona',label:'角色配置',children:config},{key:'memory',label:'经历记忆',children:memory},{key:'drafts',label:`操作记录 · ${pending.length} 项未完成`,children:drafts},{key:'manual',label:'手动操作',children:<fieldset className="agents-detail-panel" disabled={!!state.maintenance?.active} style={{border:0,minWidth:0,margin:0}}><AgentManualActions key={agent.id} agent={agent} drafts={roleDrafts} sdkAvailable={state.sdk_available}/></fieldset>},{key:'relationships',label:'社交关系',children:<div className="agents-detail-panel"><AgentRelationships relationships={agent.relationships}/></div>},{key:'inbox',label:`收到的通知 · ${inboxCount} 条未确认`,children:inbox},{key:'logs',label:'个人日志',children:<div className="agents-detail-panel"><AgentLogs key={agent.id} agentId={agent.id} agentName={agent.name}/></div>}]} /></div>;
}

export default function Agents(){
 const {agentId}=useParams<{agentId:string}>(),[searchParams,setSearchParams]=useSearchParams();
 // Every write refetches at once, so the poll only follows background work.
 // React Query already pauses it while the tab is hidden; 10 s halves the
 // team snapshots the backend builds per open page.
 const query=useApi<AgentState>('/agents',{},10000),providersQuery=useApi<{providers:Provider[];default_provider_id?:string}>('/ai/providers'),client=useQueryClient();
 const state=normalizeState(query.data),providers=(providersQuery.data?.providers||[]).filter(item=>item.enabled&&item.protocol==='openai_chat');
 const ownAccounts=useOwnAccounts();
 const maintenance=!!state.maintenance?.active;
 const platformBudgets=Object.entries(state.budgets||{}).filter(([bucket])=>!bucket.startsWith('llm:')).map(([bucket,details])=>({...details,bucket}));
 const defaultProviderId=providers.find(item=>item.id===providersQuery.data?.default_provider_id)?.id||providers[0]?.id||'';
 const providerOptions=providers.map(item=>({value:item.id,label:`${item.name} · ${item.model}`}));
 const readAction=useAction<AgentState>('/agents/read'),recoverAction=useAction<AgentState>('/agents/recover-account'),settingsAction=useAction<AgentState>('/agents/settings'),saveAction=useAction<AgentState>('/agents/save'),registerAction=useAction<AgentState>('/agents/register'),bindAction=useAction<AgentState>('/agents/bind'),runAction=useAction<AgentState>('/agents/run'),publishAction=useAction<AgentState>('/agents/publish'),discardAction=useAction<AgentState>('/agents/discard'),memoryAction=useAction<AgentState>('/agents/memory'),reconcileAction=useAction<AgentState>('/agents/reconcile'),rocketAction=useAction<AgentState>('/agents/rocket-mode');
 const resetMemoryAction=useAction<AgentState>('/agents/reset-memory'),cancelGenerationAction=useAction<AgentState>('/agents/cancel-generation');
 const confirmSkipAction=useAction<AgentState>('/agents/notifications/confirm-skip'),reconsiderNotificationAction=useAction<AgentState>('/agents/notifications/reconsider');
 const discardUnexecutedAction=useAction<AgentState>('/agents/discard-unexecuted'),queueAction=useAction<AgentState>('/agents/manual/queue'),cancelQueueAction=useAction<AgentState>('/agents/manual/queue/cancel');
 const teamTabs=['roles','drafts','inbox','logs','quotas','settings'];
 const tab=!agentId&&teamTabs.includes(searchParams.get('tab')||'')?searchParams.get('tab')!:'roles';
 function setTab(value:string){if(!agentId&&teamTabs.includes(value))setSearchParams({tab:value});}
 const [error,setError]=useState(''),[editing,setEditing]=useState<Agent|null>(null),[editorOpen,setEditorOpen]=useState(false),[accountAgent,setAccountAgent]=useState<Agent|null>(null),[accountMode,setAccountMode]=useState('register'),[draftFilter,setDraftFilter]=useState('pending'),[roleFilter,setRoleFilter]=useState(''),[memory,setMemory]=useState(''),[requestBusy,setBusy]=useState(false),[notice,context]=message.useMessage();
 const busy=requestBusy||maintenance;
 const [draftPage,setDraftPage]=useState(1),[draftPageSize,setDraftPageSize]=useState(10),draftListRef=useRef<HTMLDivElement>(null);
 const [personaBrief,setPersonaBrief]=useState(''),[personaProviderId,setPersonaProviderId]=useState(''),[generating,setGenerating]=useState(false),[generationError,setGenerationError]=useState(''),[generationStatus,setGenerationStatus]=useState('');
 const generateAction=useAIJob<GeneratedPersona>({path:'/agents/generate-persona',kind:'persona',scopeKey:editing?.id||'new'});
 const personaPending=generating||generateAction.isPending;
 const [resetMemoryConfirm,setResetMemoryConfirm]=useState(false);
 const [agentForm]=Form.useForm(),[settingsForm]=Form.useForm(),[accountForm]=Form.useForm(),locked=useRef(false),settingsLoaded=useRef(false);
 const editorSession=useRef(0),formRevision=useRef(0),generationRunning=useRef(false);
 useEffect(()=>{if(!agentId&&query.data&&!settingsLoaded.current){settingsForm.setFieldsValue({...settingsPayload(normalizeState(query.data).settings),team_code:''});settingsLoaded.current=true;}},[agentId,query.data,settingsForm]);
 useEffect(()=>{if(settingsLoaded.current)settingsForm.setFieldValue('enabled',state.settings.enabled);},[state.settings.enabled,settingsForm]);
 useEffect(()=>{if(settingsLoaded.current)settingsForm.setFieldValue('block_agent_interactions',state.settings.block_agent_interactions??true);},[state.settings.block_agent_interactions,settingsForm]);
 async function perform(action:{mutateAsync:(payload:Row)=>Promise<AgentState>},payload:Row,success:string){
  if(locked.current||maintenance)return false;locked.current=true;setBusy(true);setError('');
  try{
   const next=await action.mutateAsync(payload);
   // Writes answer with a receipt; wait for the refetch useAction started so
   // follow-up edits carry the new role version.
   if(Array.isArray(next?.agents)&&next?.settings)client.setQueryData(['/agents',{}],next);
   else await client.refetchQueries({queryKey:['/agents',{}]},{cancelRefetch:false});
   void client.invalidateQueries({predicate:query=>/^\/agents\/[^/]+\/memory$/.test(String(query.queryKey[0]))});
   if(success)notice.success(success);return true;}
  catch(error){const text=error instanceof Error?error.message:'操作失败，请稍后重试';setError(text);notice.error(text);return false;}
  finally{locked.current=false;setBusy(false);}
 }
 function openEditor(agent?:Agent){
  editorSession.current++;formRevision.current=0;generationRunning.current=false;setGenerating(false);setPersonaBrief('');setGenerationError('');setGenerationStatus('');setResetMemoryConfirm(false);
  setPersonaProviderId(defaultProviderId);
  agentForm.resetFields();agentForm.setFieldsValue(agent?{...editablePayload(agent),provider_id:agent.provider_id||''}:{name:'',username:'',background:'',stance:'支持 Vote Yes',voice:'',interests:'',language:'English',provider_id:defaultProviderId,enabled:false,mode:'auto',interval_seconds:1800,allowed_actions:allActions,freshness_minutes:60,news_freshness_hours:24,notification_interval_seconds:60});
  setEditing(agent||null);setMemory(agent?.memory||'');setError('');setEditorOpen(true);
 }
 function closeEditor(){editorSession.current++;generationRunning.current=false;setGenerating(false);setEditorOpen(false);setResetMemoryConfirm(false);}
 async function resetAgentMemory(){
  if(!editing||locked.current||generationRunning.current||generateAction.isPending||generationActive(agents.find(agent=>agent.id===editing.id)||editing))return;
  const session=editorSession.current,agentId=editing.id;
  if(await perform(resetMemoryAction,{agent_id:agentId},'记忆与阅读历史已清空，恢复副本已保存')&&session===editorSession.current){
   setMemory('');setResetMemoryConfirm(false);
   const latest=normalizeState(client.getQueryData<Row>(['/agents',{}])).agents.find(agent=>agent.id===agentId);
   if(latest)setEditing(current=>current?.id===agentId?{...current,version:latest.version}:current);
  }
 }
 async function generatePersona(){
  if(generationRunning.current||generateAction.isPending||locked.current||maintenance||!providers.length)return;
  const session=editorSession.current,revision=formRevision.current,values=agentForm.getFieldsValue(),providerId=personaProviderId;
  if(providerId&&!providers.some(provider=>provider.id===providerId)){setGenerationError('所选 AI 服务暂不可用，请选择已启用的文字服务。');return;}
  generationRunning.current=true;setGenerating(true);setGenerationError('');setGenerationStatus('');
  try{
   const result=await generateAction.mutateAsync({brief:personaBrief.trim(),provider_id:providerId,...(editing?{existing_agent_id:editing.id}:{}),persona:personaPayload(values)});
   if(session!==editorSession.current)return;
   if(revision!==formRevision.current){setGenerationStatus('生成期间你修改了角色设定，本次结果未覆盖填写内容。可按当前内容重新生成。');return;}
   applyGeneratedPersona(result);
  }catch(error){if(session===editorSession.current)setGenerationError(error instanceof Error?error.message:'角色生成失败，请稍后重试。');}
  finally{if(session===editorSession.current){generationRunning.current=false;setGenerating(false);}}
 }
 function applyGeneratedPersona(result:GeneratedPersona){
   const persona=result.persona;
   if(!persona||typeof persona.background!=='string'||!persona.background.trim())throw new Error('AI 未返回完整的角色背景，请重新生成。');
   const next=Object.fromEntries(personaFields.flatMap(key=>{
    if(key==='username'&&usernameIsBound(editing))return [];
    const value=persona[key];
    return typeof value==='string'?[[key,value]]:key==='interests'&&Array.isArray(value)?[[key,value.filter(item=>typeof item==='string').join('\n')]]:[];
   }));
   agentForm.setFieldsValue({...next,language:'English'});formRevision.current++;
   setGenerationStatus('AI 已填写角色设定，可继续修改下方内容，确认后保存角色。');
 }
 async function saveAgent(){
  if(locked.current||generationRunning.current||generateAction.isPending)return;
  try{const values=await agentForm.validateFields();if(await perform(saveAction,{...editablePayload(values),...(editing?{id:editing.id,version:editing.version}:{})},editing?'角色已更新':'角色已创建'))closeEditor();}
  catch(error){if(!validationError(error))setError(error instanceof Error?error.message:'请检查角色设置');}
 }
 async function saveSettings(){
  if(locked.current)return;
  try{const values=await settingsForm.validateFields();const payload={...settingsPayload(values as Settings),...(values.team_code?.trim()?{team_code:values.team_code.trim()}:{})};if(await perform(settingsAction,payload,'队伍设置已保存'))settingsForm.setFieldValue('team_code','');}
  catch(error){if(!validationError(error))setError(error instanceof Error?error.message:'请检查队伍设置');}
 }
 function openAccount(agent:Agent){setAccountAgent(agent);setAccountMode(agent.account_id||agent.account_state&&agent.account_state!=='unbound'?'bind':'register');accountForm.resetFields();accountForm.setFieldsValue({username:agent.username||'',password:''});setError('');}
 async function connectAccount(){
  if(locked.current||!accountAgent)return;
  try{const values=await accountForm.validateFields();const payload=accountMode==='register'?{agent_id:accountAgent.id,...(values.password?{password:values.password}:{})}:{agent_id:accountAgent.id,username:values.username.trim(),password:values.password};if(await perform(accountMode==='register'?registerAction:bindAction,payload,accountMode==='register'?'账号已创建并连接':'账号已连接')){accountForm.resetFields();setAccountAgent(null);}}
  catch(error){if(!validationError(error))setError(error instanceof Error?error.message:'账号连接失败');}
 }
 const agents=state.agents||[],drafts=state.drafts||[],pending=drafts.filter(draft=>unfinishedDraft(draft,agents.find(agent=>agent.id===draft.agent_id))),linked=agents.filter(agent=>agent.account_id),enabled=agents.filter(agent=>agent.enabled);
 const detailAgent=agentId?agents.find(agent=>agent.id===agentId):undefined;
 const focusedDraft=searchParams.get('draft')||'',detailTabs=['overview','persona','memory','drafts','manual','relationships','inbox','logs'],detailTab=detailTabs.includes(searchParams.get('tab')||'')?searchParams.get('tab')!:'overview';
 const inbox=agents.filter(agent=>!agentId||agent.id===agentId).flatMap(agent=>(agent.inbox||[]).map(item=>({...item,agent}))).sort((a,b)=>String(b.created_at||b.observed_at||'').localeCompare(String(a.created_at||a.observed_at||'')));
 const unresolved=inbox.filter(notificationUnfinished);
 const filteredDrafts=drafts.filter(draft=>(!agentId||draft.agent_id===agentId)&&(!!agentId||!roleFilter||draft.agent_id===roleFilter)&&(focusedDraft?draft.id===focusedDraft:draftFilter==='all'||draftFilter==='pending'&&unfinishedDraft(draft,agents.find(agent=>agent.id===draft.agent_id))||draft.status===draftFilter));
 const draftPageCount=Math.max(1,Math.ceil(filteredDrafts.length/draftPageSize)),currentDraftPage=Math.min(draftPage,draftPageCount);
 const pagedDrafts=filteredDrafts.slice((currentDraftPage-1)*draftPageSize,currentDraftPage*draftPageSize);
 useEffect(()=>{setDraftPage(1);},[agentId,roleFilter,draftFilter,focusedDraft]);
 useEffect(()=>{setDraftPage(page=>Math.min(page,draftPageCount));},[draftPageCount]);
 function changeDraftPage(page:number,pageSize:number){
  setDraftPage(pageSize===draftPageSize?page:1);setDraftPageSize(pageSize);
  draftListRef.current?.scrollIntoView({behavior:'smooth',block:'start'});
 }
 const draftPagination=(position:'顶部'|'底部')=><nav className="agents-draft-pagination" aria-label={`操作记录分页（${position}）`}><Pagination current={currentDraftPage} pageSize={draftPageSize} total={filteredDrafts.length} showSizeChanger pageSizeOptions={[10,20,50]} showQuickJumper responsive disabled={requestBusy} onChange={changeDraftPage} showTotal={(total,range)=>`第 ${range[0]}–${range[1]} 条，共 ${total} 条`}/></nav>;
 const isRunning=(agent:Agent)=>agentRunning(agent,state.running);
 const roleItems=<div className="stack">
  <div className="agents-section-head"><div><h3>角色工作区</h3><p className="muted">每个角色依据自己的经历与记忆，阅读近期消息并选择合适的互动。</p></div><Button type="primary" disabled={busy} onClick={()=>openEditor()}>新建角色</Button></div>
  <RiskSummary risks={ownAccounts.risks}/>
  {!agents.length?<div className="agents-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="创建第一个角色，让它开始阅读与互动"/><Button disabled={busy} onClick={()=>openEditor()}>配置角色身份</Button></div>:<div className="agents-grid">{agents.map(agent=><article className="agents-role" key={agent.id}>
   <div className="agents-section-head"><div className="agents-role-title"><span className="agents-avatar" aria-hidden="true">{(agent.name||'A').slice(0,1)}</span><div><h3>{agent.name}</h3><span className="muted">@{agent.username||'未设置用户名'}</span></div></div><Tag color={leaseRecovery(agent)?'orange':isRunning(agent)?'processing':agent.enabled?'green':'default'}>{isRunning(agent)?runningLabel(agent):agent.enabled?'已启用':'已暂停'}</Tag></div>
   <div className="agents-role-tags"><Tag>{agent.mode==='auto'?'自动执行':'审阅后执行'}</Tag><Tag color={agent.account_id?'green':'default'}>{agent.account_id?`账号 #${agent.account_id}`:'未连接账号'}</Tag>{agent.account_suspended&&<Tag color="red">已封禁</Tag>}<RiskTag risk={ownAccounts.risk(agent.account_id)}/><IdentityTag row={ownAccounts.identity(agent.account_id)}/></div>
   <p className="agents-role-background">{agent.background||'尚未设置身份背景'}</p>
   <dl className="agents-role-details"><div><dt>立场</dt><dd>{agent.stance||'尚未设置'}</dd></div><div><dt>兴趣</dt><dd>{agent.interests||'尚未设置'}</dd></div><div><dt>无新消息时检查</dt><dd>每 {Number(((agent.interval_seconds||1800)/60).toFixed(1))} 分钟</dd></div><div><dt>新鲜</dt><dd>帖子 {agent.freshness_minutes} 分钟 · 新闻采集 {agent.news_freshness_hours} 小时</dd></div></dl>
   <Space size={[0,5]} wrap>{(agent.allowed_actions||legacyActions).map(action=><Tag key={action}>{actionLabels[action]}</Tag>)}</Space><ObservationSummary agent={agent}/><AgentPerformance agent={agent}/>
   <div className="agents-role-time"><span>上次阅读 <b>{time(agent.last_read)}</b></span><span>空闲检查 <b>{agent.enabled&&state.settings.enabled?time(agent.next_run):'已暂停'}</b></span><span>通知待处理 <b>{(agent.inbox||[]).filter(notificationUnfinished).length} 条</b></span></div>
   {currentAgentError(agent)&&<p className="agents-inline-error">{currentAgentError(agent)}</p>}
   <GenerationProgress agent={agent}/>
   <div className="agents-role-actions"><Space wrap><Button size="small" disabled={busy||isRunning(agent)} onClick={()=>void perform(readAction,{agent_id:agent.id},'已更新阅读与通知记录')}>只阅读</Button><Button size="small" title={providers.length?'根据人设与记忆决定本轮操作':'先在 AI 配置中配置文字服务'} disabled={busy||isRunning(agent)||!providers.length} loading={generationActive(agent)&&!leaseRecovery(agent)} onClick={()=>void perform(runAction,{agent_id:agent.id},'阅读与决策已提交，完成后会自动更新')}>阅读并决策</Button>{generationActive(agent)&&!leaseRecovery(agent)&&agent.generation?.stage!=='publishing'&&<Button size="small" disabled={busy||agent.generation?.status==='cancelling'} loading={cancelGenerationAction.isPending} onClick={()=>void perform(cancelGenerationAction,{agent_id:agent.id},'已请求取消，正在等待任务结束')}>{agent.generation?.status==='cancelling'?'正在取消':'取消本次决策'}</Button>}<Link className="agents-manage-link" to={`/ai/agents/${encodeURIComponent(agent.id)}`}>完整管理</Link><Button size="small" disabled={busy} onClick={()=>openEditor(agent)}>编辑角色</Button><Button size="small" disabled={busy||!state.sdk_available} onClick={()=>openAccount(agent)}>{agent.account_id?'账号设置':'连接账号'}</Button>{agent.account_state==='registration_uncertain'&&<Button size="small" disabled={busy||isRunning(agent)} onClick={()=>void perform(recoverAction,{agent_id:agent.id},'已核对原账号')}>核对原账号</Button>}</Space></div>
  </article>)}</div>}
 </div>;
 const draftItems=<div className="stack agents-draft-list" ref={draftListRef}><div className="agents-section-head"><div><h3>操作审阅</h3><p className="muted">计数为待审阅、待执行、执行失败、待核对及待确认跳过的未完成项目，不是累计完成次数。</p></div><Space wrap>{!agentId&&<Select aria-label="筛选角色草稿" value={roleFilter} onChange={setRoleFilter} style={{minWidth:150}} options={[{value:'',label:'全部角色'},...agents.map(agent=>({value:agent.id,label:agent.name}))]}/>}<Select aria-label="筛选草稿状态" value={focusedDraft?'all':draftFilter} onChange={value=>{setDraftFilter(value);if(focusedDraft)setSearchParams({tab:'drafts'});}} style={{minWidth:130}} options={[{value:'pending',label:'待处理'},{value:'published',label:'已完成'},{value:'skipped',label:'已跳过'},{value:'discarded',label:'已放弃'},{value:'all',label:'全部状态'}]}/></Space></div>
  {!!filteredDrafts.length&&draftPagination('顶部')}
  {!filteredDrafts.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有符合条件的操作"/>:pagedDrafts.map(draft=><DraftCard key={draft.id} draft={draft} agent={agents.find(agent=>agent.id===draft.agent_id)} busy={busy} onPublish={(id,body,confirmationPostId)=>perform(publishAction,{draft_id:id,body,...(confirmationPostId?{confirmation_post_id:confirmationPostId}:{})},'操作结果已更新')} onDiscard={id=>perform(discardAction,{draft_id:id},'已放弃操作')} onConfirmUnexecuted={(id,reason,version)=>perform(discardUnexecutedAction,{draft_id:id,reason,expected_version:version,confirmed_not_executed:true},'已记录人工确认并放弃操作')} onReconcile={(id,postId)=>perform(reconcileAction,{draft_id:id,...(postId?{post_id:postId}:{})},'操作结果已核对')} onQueue={(id,body)=>perform(queueAction,{draft_id:id,body},'已排队：有发布名额时自动发送一次')} onCancelQueue={id=>perform(cancelQueueAction,{draft_id:id},'已取消排队，草稿保留为待确认')}/>)}
  {filteredDrafts.length>draftPageSize&&draftPagination('底部')}
 </div>;
 const inboxItems=<div className="stack"><div className="agents-section-head"><div><h3>通知收件箱</h3><p className="muted">收到回复或提及后，角色可以判断、审阅或执行操作；平台确认单独记录。</p></div><Tag>{unresolved.length} 条未确认</Tag></div><Table rowKey={item=>`${item.agent.id}-${item.id}`} size="small" dataSource={inbox} scroll={{x:1200}} locale={{emptyText:'尚无通知，角色阅读时会检查新消息'}} pagination={{pageSize:15,showSizeChanger:false}} columns={[
  {title:'角色',key:'agent',width:130,render:(_,item)=>item.agent.name},
  {title:'收到的通知',key:'kind',width:160,render:(_,item)=><Space><Tag>{item.kind==='reply'?'收到回复':item.kind==='mention'?'被提及':item.kind||'通知'}</Tag><span className="muted">#{item.notification_id??item.id}</span></Space>},
  {title:'本地进展',key:'progress',width:190,render:(_,item)=><span title={item.policy_reason}>{notificationIgnored(item)?'内部互动，已忽略':notificationProgressLabels[item.local_progress||'awaiting_decision']||'待判断'}</span>},
  {title:'关联动作',key:'action',width:150,render:(_,item)=>item.related_draft_id?<Link to={`/ai/agents/${item.agent.id}?tab=drafts&draft=${item.related_draft_id}`}>{actionLabels[item.related_action as ActionType]||'查看操作'} · {draftLabels[({pending:'draft',rejected:'failed'} as Record<string,string>)[item.related_draft_state||'']||item.related_draft_state||'']||'查看详情'}</Link>:<span className="muted">尚无选定操作</span>},
  {title:'平台确认',dataIndex:'status',width:110,render:value=><Tag color={value==='resolved'?'green':value==='ack_pending'?'processing':'orange'}>{value==='resolved'?'已确认':value==='ack_pending'?'确认中':'未确认'}</Tag>},
  {title:'来源',key:'source',width:210,render:(_,item)=><Space direction="vertical" size={2}>{item.post_id?<Link to={`/posts?post=${item.post_id}`}>通知原帖 #{item.post_id} ↗</Link>:<span className="muted">没有关联帖子</span>}{item.source_id&&<span className="muted">资料 {item.source_id}</span>}</Space>},
  {title:'收到时间',key:'at',width:170,render:(_,item)=>time(timestamp(item.created_at||item.observed_at))},
  {title:'平台确认时间',dataIndex:'resolved_at',width:170,render:value=>time(timestamp(value))},
  {title:'后续处理',key:'next',width:160,render:(_,item)=>notificationIgnored(item)?<span className="muted">无需处理</span>:<Space direction="vertical">{item.can_confirm_skip&&item.related_draft_id&&<Button size="small" disabled={busy||generationActive(item.agent)} onClick={()=>void perform(confirmSkipAction,{draft_id:item.related_draft_id},'跳过已确认，平台状态会单独更新')}>确认跳过通知</Button>}{item.can_reconsider&&<Button size="small" disabled={busy||generationActive(item.agent)} onClick={()=>void perform(reconsiderNotificationAction,{agent_id:item.agent.id,notification_id:item.notification_id??Number(item.id)},'通知已重新开放判断，原已放弃动作不会直接重放')}>重新判断通知</Button>}</Space>},
 ]}/><p className="muted">重新判断会允许角色考虑其他想法；已放弃的同一动作不会直接重放。</p></div>;
 const settingItems=<section className="agents-settings"><h3>队伍与统一调度</h3><p className="muted">所有角色共享赛事平台额度；调度器会在新消息和通知到达时安排角色阅读与决策。</p><Form form={settingsForm} layout="vertical" disabled={busy} onFinish={()=>void saveSettings()} initialValues={{enabled:false,team_side:'Vote Yes',max_accounts:10,block_agent_interactions:true,team_code:''}}><Form.Item name="enabled" label="持续运行" valuePropName="checked"><Switch checkedChildren="运行" unCheckedChildren="暂停"/></Form.Item><Form.Item name="block_agent_interactions" label="禁止 Agent 内部互动" valuePropName="checked" extra={<span>禁止受管 Agent 之间回复、点赞、关注、转发及正文 @提及；人工取消点赞和取消关注仍可用于清理历史关系。{state.interaction_policy&&<><br/>今日已拦截 {state.interaction_policy.blocked_today} 次。</>}</span>}><Switch checkedChildren="开启" unCheckedChildren="关闭"/></Form.Item><div className="agents-form-grid"><Form.Item name="team_side" label="队伍立场"><Input placeholder="Vote Yes" maxLength={80}/></Form.Item><Form.Item name="max_accounts" label="角色数量上限" rules={[{required:true,message:'请设置账号数量上限'},{type:'integer',min:1,max:50,message:'请输入 1–50 之间的整数'}]}><InputNumber min={1} max={50} precision={0} style={{width:'100%'}}/></Form.Item></div><p className="muted">当前队伍冷却至：{time(timestamp(state.cooldown_until))}</p><Form.Item name="team_code" label={<Space>团队码{state.has_team_code&&<Tag color="green">已保存</Tag>}</Space>}><Input.Password maxLength={500} autoComplete="new-password" placeholder={state.has_team_code?'留空保留已保存的团队码':'填写赛事团队码'}/></Form.Item><Button type="primary" htmlType="submit" loading={settingsAction.isPending} disabled={busy}>保存队伍设置</Button></Form></section>;
 return <div className="agents-page stack">{context}<div className="agents-toolbar"><div><h2>{agentId?`${detailAgent?.name||'角色'} · 管理`:'Agent 管理'}</h2>{agentId?<p><Link to="/ai/agents">← 返回全部角色</Link></p>:<p>让每个角色拥有自己的背景、观点与日常阅读节奏。</p>}</div><Space wrap><Link to="/ai/reading">资料筛选 ↗</Link><span className="agents-run-label">持续运行</span><Switch aria-label="持续运行所有已启用角色" checked={!!state.settings.enabled} disabled={busy||!query.data} loading={settingsAction.isPending} checkedChildren="运行" unCheckedChildren="暂停" onChange={value=>{void perform(settingsAction,{enabled:value},value?'已开启持续运行':'已暂停持续运行').then(saved=>{if(saved&&!agentId)settingsForm.setFieldValue('enabled',value);});}}/><span className="agents-run-label">🚀 火箭模式</span><Switch aria-label="火箭模式：全员启用且不设任何限制" checked={!!state.settings.rocket_mode} disabled={busy||!query.data} loading={rocketAction.isPending} checkedChildren="全开" unCheckedChildren="常规" onChange={value=>{void perform(rocketAction,{on:value},value?'火箭模式已开启：全员启用、不设任何限制':'火箭模式已关闭：已还原');}}/></Space></div>
  {maintenance&&<Alert type="info" showIcon title="系统维护中" description={`${state.maintenance?.reason||"正在修复运行状态。"} 维护完成前暂停新增操作，页面记录仍可查看。`}/>}
  {query.isError&&<Alert type="error" showIcon title="Agent 数据读取失败" description={query.error.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>}
  {error&&<Alert type="error" showIcon title={error} closable onClose={()=>setError('')}/>}
  {query.isPending?<Skeleton active paragraph={{rows:8}}/>:agentId?detailAgent?<AgentWorkspace agent={detailAgent} state={state} busy={busy} running={isRunning(detailAgent)} providerReady={!!providers.length} tab={detailTab} onTab={value=>setSearchParams({tab:value})} onToggle={()=>void perform(saveAction,{...editablePayload(detailAgent),id:detailAgent.id,version:detailAgent.version,enabled:!detailAgent.enabled},detailAgent.enabled?'角色已暂停':'角色已启用')} onRead={()=>void perform(readAction,{agent_id:detailAgent.id},'已更新阅读与通知记录')} onRun={()=>void perform(runAction,{agent_id:detailAgent.id},'阅读与决策已提交，完成后会自动更新')} onCancel={()=>void perform(cancelGenerationAction,{agent_id:detailAgent.id},'已请求取消，正在等待任务结束')} onEditor={()=>openEditor(detailAgent)} onAccount={()=>openAccount(detailAgent)} drafts={draftItems} inbox={inboxItems}/>:<Alert type="warning" showIcon title="角色不存在或已不可用" description={<Link to="/ai/agents">返回角色列表</Link>}/>:<><div className="agents-overview"><div><span>角色总数</span><strong>{agents.length}</strong><small>{enabled.length} 个已启用</small></div><div><span>已连接账号</span><strong>{linked.length}<small> / {state.settings.max_accounts??10}</small></strong><small>独立身份与记忆</small></div><button type="button" onClick={()=>{setTab('drafts');setDraftFilter('pending');}}><span>未完成操作</span><strong>{pending.length}</strong><small>{pending.filter(draft=>draft.status==='uncertain').length} 条待核对</small></button><div><span>工作模式</span><strong className="agents-overview-mode">{state.settings.enabled?'持续运行':'已暂停'}</strong><small>{state.settings.enabled?'按角色节奏阅读与写作':'仍可手动阅读并生成'}</small></div></div>
  {!state.sdk_available&&<Alert type="info" showIcon title="账号连接暂不可用" description="你可以先配置角色并阅读资料、生成草稿。"/>}
  {!providersQuery.isPending&&!providersQuery.isError&&!providers.length&&<Alert type="info" title="先配置角色并阅读资料" description={<span>生成内容前，请在 <Link to="/ai/settings">AI 配置</Link> 中配置文字服务。</span>}/>}{providersQuery.isError&&<Alert type="error" showIcon title="AI 服务配置读取失败" description={providersQuery.error.message} action={<Button onClick={()=>void providersQuery.refetch()}>重试</Button>}/>}
  {!!platformBudgets.length&&<Collapse size="small" items={[{key:'budgets',label:'赛事平台额度与冷却',children:<Table<Row> size="small" rowKey="bucket" pagination={false} scroll={{x:500}} dataSource={platformBudgets} columns={[{title:'请求类型',dataIndex:'bucket',render:value=>budgetLabel(String(value))},{title:'剩余额度',key:'remaining',render:(_,row)=>row.remaining==null?'未返回':`${row.remaining}${row.limit==null?'':` / ${row.limit}`}`},{title:'调度状态',dataIndex:'retry_after',render:value=>Number(value)>0?`约 ${Math.ceil(Number(value))} 秒后继续`:'可调度'},{title:'最近请求',dataIndex:'last_attempt_at',render:value=>time(timestamp(value))}]}/>}]} />}
  <Tabs activeKey={tab} onChange={setTab} items={[{key:'roles',label:`角色 · ${agents.length}`,children:roleItems},{key:'drafts',label:`操作记录 · ${pending.length} 项未完成`,children:draftItems},{key:'inbox',label:`收到的通知 · ${unresolved.length} 条未确认`,children:inboxItems},{key:'logs',label:'运行记录',children:<div className="stack"><div className="agents-section-head"><div><h3>最近运行记录</h3><p className="muted">角色阅读、决策和互动的最新动态。</p></div></div><Table<Row> rowKey={(event,index)=>`${event.at}-${event.agent_id}-${index}`} size="small" dataSource={state.events||[]} scroll={{x:620}} locale={{emptyText:'尚无运行记录'}} pagination={{pageSize:15,showSizeChanger:false}} columns={[{title:'时间',dataIndex:'at',width:170,render:value=>time(timestamp(value))},{title:'角色',dataIndex:'agent_id',width:140,render:value=>agents.find(agent=>agent.id===value)?.name||'队伍'},{title:'动态',dataIndex:'message',render:value=><span className="agents-log-message">{value||'—'}</span>}]}/></div>},{key:'quotas',label:'额度管理',children:tab==='quotas'?<fieldset className="stack" disabled={maintenance} style={{border:0,minWidth:0,padding:0,margin:0}}><AgentCompetitionTrial agents={agents} teamEnabled={state.settings.enabled}/><AgentQuotas/></fieldset>:null},{key:'settings',label:'队伍设置',children:settingItems}]}/></>}
  <Drawer title={editing?`编辑角色 · ${editing.name}`:'新建角色'} open={editorOpen} width={760} onClose={()=>{if(!requestBusy)closeEditor();}} maskClosable={!requestBusy} closable={!requestBusy} extra={<Button type="primary" loading={saveAction.isPending} disabled={busy||personaPending} onClick={()=>void saveAgent()}>保存角色</Button>} className="agents-drawer"><div className="stack">{error&&<Alert type="error" title={error} showIcon/>}
   <Form form={agentForm} layout="vertical" disabled={busy} onValuesChange={()=>{formRevision.current++;setGenerationStatus('');}}>
    <section className="agents-runtime-ai" aria-label="角色决策写作 AI 设置"><div className="agents-section-head"><h3>决策与写作 AI</h3><Link to="/ai/settings">管理 AI 服务 ↗</Link></div><Form.Item name="provider_id" label="角色决策写作 AI" extra={<span>用于角色决策与写作，点击「保存角色」后生效。资料筛选在 <Link to="/ai/reading">公共资料筛选</Link> 中单独配置。</span>}><Select placeholder="选择角色决策写作服务" options={[{value:'',label:'跟随默认 AI 服务'},...providerOptions]} notFoundContent="请先在 AI 配置中添加并启用服务"/></Form.Item></section>
    <section className="agents-persona-generator"><div><h3>{editing?'让角色经历更完整':'从一个想法开始创建角色'}</h3><p>{editing?'AI 会结合已有设定，补充成长经历、人生转折、立场由来与性格细节。':'写一句想法，让 AI 补全角色的经历、立场和表达习惯；留空可自动构思。'}</p></div>
     <Form.Item label="角色构想（可选）"><Input.TextArea aria-label="角色构想（可选）" value={personaBrief} onChange={event=>{setPersonaBrief(event.target.value);formRevision.current++;setGenerationStatus('');}} autoSize={{minRows:2,maxRows:5}} maxLength={2000} placeholder={editing?'例如：保留图书管理员身份，补充一次改变其教育观念的家庭经历。':'例如：一位做过夜班护士、后来经营社区花店的中年女性，温和但有自己的坚持。'}/></Form.Item>
     <div className="agents-persona-generate-actions"><Form.Item label="背景生成 AI"><Select aria-label="背景生成 AI" value={personaProviderId} onChange={value=>{setPersonaProviderId(value);setGenerationError('');setGenerationStatus('');}} disabled={busy||personaPending} placeholder="选择背景生成服务" options={[{value:'',label:'使用默认 AI 服务'},...providerOptions]} notFoundContent="请先在 AI 配置中添加并启用服务"/></Form.Item><Button type="primary" loading={personaPending} disabled={busy||personaPending||!providers.length} onClick={()=>void generatePersona()}>{personaPending?'正在构思角色…':editing?'AI 丰富角色背景':'AI 自动生成角色'}</Button></div>
     {personaPending&&<Alert type="info" showIcon title={generateAction.stage||'正在构思角色'} description={`已等待 ${Math.max(0,Math.floor(generateAction.elapsedSeconds))} 秒。关闭面板或刷新页面不会终止任务，回来后可继续查看。`} action={<Button onClick={()=>void generateAction.cancel().catch(error=>setGenerationError(error instanceof Error?error.message:'取消失败，请重试'))}>取消人设生成</Button>}/>}
     {!personaPending&&generateAction.data&&!generationStatus&&<Button onClick={()=>{try{applyGeneratedPersona(generateAction.data!);}catch(error){setGenerationError(error instanceof Error?error.message:'无法应用生成结果');}}}>应用已完成的人设</Button>}
     <p className="agents-persona-hint">仅用于本次生成或丰富背景，独立于上方的角色决策写作 AI。</p>
     {!providersQuery.isPending&&!providers.length&&<p className="agents-persona-hint">请先在 <Link to="/ai/settings">AI 配置</Link> 中添加并启用文字服务，也可以手动填写角色。</p>}
     {generationError&&<Alert type="error" title={generationError} showIcon/>}{generationStatus&&<Alert type="info" title={generationStatus} showIcon/>}
    </section>
    <div className="agents-form-grid"><Form.Item name="name" label="角色名称" rules={[{required:true,whitespace:true,message:'请填写角色名称'}]}><Input placeholder="角色的显示名称" maxLength={80}/></Form.Item><Form.Item name="username" label="账号用户名" rules={[{required:true,whitespace:true,message:'请填写账号用户名'},{pattern:/^[A-Za-z0-9_]{3,30}$/,message:'请输入 3–30 个英文字母、数字或下划线'}]}><Input placeholder="用于创建赛事账号" maxLength={30} autoComplete="off" disabled={usernameIsBound(editing)}/></Form.Item></div>
    <Form.Item name="background" label="身份与历史背景" className="agents-background-field" extra="可以分段写成长与家庭、职业与日常、关键转折、关系牵挂，以及经历如何塑造观点和内心矛盾。" rules={[{required:true,whitespace:true,message:'请填写角色背景'}]}><Input.TextArea autoSize={{minRows:10,maxRows:24}} placeholder={'成长与家庭：在哪里长大，谁影响过自己。\n\n职业与关键经历：做过什么，经历过哪些改变人生的事。\n\n现在的生活：在意谁、担心什么，又有哪些矛盾和未完成的心愿。'} maxLength={3000} showCount/></Form.Item><Form.Item name="stance" label="个人立场" extra="写清观点背后的个人原因，也保留疑虑、分歧和愿意改变想法的条件。"><Input.TextArea autoSize={{minRows:4,maxRows:10}} placeholder="关心的问题、支持与反对的具体理由、能够接受的取舍，以及仍在犹豫的地方。" maxLength={2000} showCount/></Form.Item><Form.Item name="voice" label="表达习惯" extra="像日常交谈一样表达，避免破折号、花哨符号和模板口号。"><Input.TextArea autoSize={{minRows:3,maxRows:8}} placeholder="如何措辞，常用哪些生活例子，遇到不同意见如何回应；可以写一两句典型表达。" maxLength={1000} showCount/></Form.Item><Form.Item name="interests" label="关注的话题" extra="每行一个话题，也可以补充角色特别关注的切入点。"><Input.TextArea autoSize={{minRows:3,maxRows:6}} placeholder={'社区教育：关注普通家庭的负担\n公共交通：留意夜班工作者的出行'} maxLength={1000}/></Form.Item>
    <Form.Item name="language" label="主要语言" extra="本队对外发言固定 English only，表情正常允许；人设背景与后台理由可继续使用中文。"><Input value="English" readOnly aria-readonly="true"/></Form.Item>
    <div className="agents-form-divider"><h3>阅读与通知</h3><div className="agents-form-grid">
     <Form.Item name="interval_seconds" label="无新消息时的检查间隔（秒）" extra="新消息或通知到达时可提前检查；此间隔用于没有新内容时的轮询。" rules={[{required:true,message:'请填写检查间隔'},{type:'integer',min:25,max:86400,message:'请输入 25–86400 之间的整数'}]}><InputNumber min={25} max={86400} precision={0} style={{width:'100%'}}/></Form.Item>
     <Form.Item name="notification_interval_seconds" label="通知检查间隔（秒）" rules={[{required:true,message:'请填写通知检查间隔'},{type:'integer',min:30,max:3600,message:'请输入 30–3600 之间的整数'}]}><InputNumber min={30} max={3600} precision={0} style={{width:'100%'}}/></Form.Item>
     <Form.Item name="freshness_minutes" label="近期帖子窗口（分钟）" rules={[{required:true,message:'请填写近期帖子窗口'},{type:'integer',min:5,max:1440,message:'请输入 5–1440 之间的整数'}]}><InputNumber min={5} max={1440} precision={0} style={{width:'100%'}}/></Form.Item>
     <Form.Item name="news_freshness_hours" label="新闻采集新鲜窗口（小时）" extra="按真实采集时间判断，新闻剧情日期单独显示。" rules={[{required:true,message:'请填写新闻新鲜窗口'},{type:'integer',min:1,max:168,message:'请输入 1–168 之间的整数'}]}><InputNumber min={1} max={168} precision={0} style={{width:'100%'}}/></Form.Item>
    </div></div>
    <div className="agents-form-divider"><h3>操作与运行</h3><Form.Item name="allowed_actions" label="允许角色选择的动作" rules={[{required:true,type:'array',min:1,message:'请至少选择一种动作'}]}><Select mode="multiple" options={allActions.map(action=>({value:action,label:actionLabels[action]}))}/></Form.Item><div className="agents-form-grid"><Form.Item name="enabled" label="启用此角色" extra="完成账号和 AI 配置后启用。" valuePropName="checked"><Switch/></Form.Item><Form.Item name="mode" label="操作方式" extra="自动模式由角色决定并执行；审阅模式由你确认后执行。"><Select options={[{value:'auto',label:'决策后自动执行'},{value:'review',label:'生成操作，审阅后执行'}]}/></Form.Item></div></div>
   </Form>
   {editing&&<section className="agents-memory"><h3>手动补充的长期笔记</h3><p className="muted">可以补充希望角色记住的想法与关注点。角色自动形成的观点、疑问和人物印象显示在下方。</p><p className="muted">笔记与想法不代表操作已经完成，发帖和回复请查看实际操作记录。</p><Input.TextArea aria-label="手动补充的长期笔记" value={memory} disabled={busy} onChange={event=>setMemory(event.target.value)} autoSize={{minRows:5,maxRows:15}} maxLength={4000}/><Space wrap><Button disabled={busy||memory===(agents.find(agent=>agent.id===editing.id)?.memory||'')} loading={memoryAction.isPending} onClick={()=>void perform(memoryAction,{agent_id:editing.id,memory},'长期笔记已保存')}>保存笔记</Button><Button danger disabled={busy||personaPending||generationActive(agents.find(agent=>agent.id===editing.id)||editing)||resetMemoryConfirm} onClick={()=>setResetMemoryConfirm(true)}>清空记忆与阅读历史</Button></Space><p className="muted">清空已读内容、往来线索、想法和经历，保留账号、人设、实际操作审计与待核对状态。清理前会保存本地恢复副本。</p>{resetMemoryConfirm&&<div className="agents-memory-reset" aria-label="确认清空角色记忆"><p>确认清空 {editing.name} 的记忆与阅读历史？这不会清空正在编辑的人设。</p><Space><Button disabled={busy} onClick={()=>setResetMemoryConfirm(false)}>取消清空</Button><Button danger type="primary" disabled={busy||personaPending||generationActive(agents.find(agent=>agent.id===editing.id)||editing)} loading={resetMemoryAction.isPending} onClick={()=>void resetAgentMemory()}>确认清空</Button></Space></div>}</section>}
   {editing&&<RoleMemory agent={agents.find(agent=>agent.id===editing.id)||editing}/>}
   {editing&&<ObservationSummary agent={agents.find(agent=>agent.id===editing.id)||editing}/>}
  </div></Drawer>
  <Modal title={`${accountAgent?.name||'角色'} · 连接账号`} open={!!accountAgent} onCancel={()=>{if(!requestBusy){setAccountAgent(null);accountForm.resetFields();}}} onOk={()=>void connectAccount()} okText={accountMode==='register'?'创建并连接':'连接账号'} confirmLoading={registerAction.isPending||bindAction.isPending} okButtonProps={{disabled:busy||accountMode==='register'&&!state.has_team_code}} cancelButtonProps={{disabled:requestBusy}} closable={!requestBusy} maskClosable={!requestBusy} destroyOnHidden><div className="agents-account stack">{error&&<Alert type="error" title={error} showIcon/>}<Select aria-label="账号连接方式" value={accountMode} disabled={busy} options={[{value:'register',label:'自动创建新账号',disabled:!!accountAgent?.account_state&&accountAgent.account_state!=='unbound'},{value:'bind',label:'连接已有账号'}]} onChange={value=>{setAccountMode(value);accountForm.setFieldValue('password','');}}/>{accountMode==='register'?<><p>使用角色用户名 <strong>@{accountAgent?.username}</strong> 创建本队赛事账号。</p>{!state.has_team_code&&<Alert type="warning" title="请先在队伍设置中保存团队码" showIcon/>}</>:<p className="muted">使用已有赛事账号继续这个角色的阅读与写作。</p>}<Form form={accountForm} layout="vertical" disabled={busy} preserve={false}>{accountMode==='bind'&&<Form.Item name="username" label="账号用户名" rules={[{required:true,whitespace:true,message:'请填写用户名'}]}><Input autoComplete="off"/></Form.Item>}<Form.Item name="password" label={accountMode==='register'?'账号密码（留空自动生成）':'账号密码'} rules={accountMode==='bind'?[{required:true,message:'请填写密码'}]:[{min:8,message:'账号密码至少 8 个字符'}]}><Input.Password maxLength={200} autoComplete="new-password"/></Form.Item></Form></div></Modal>
 </div>;
}

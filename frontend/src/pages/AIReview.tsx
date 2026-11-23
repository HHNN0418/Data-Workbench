import {useEffect, useState} from 'react';
import {Link} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import {Alert, Button, Collapse, Empty, Select, Skeleton, Space, Switch, Table, Tabs, Tag} from 'antd';
import {useAction, useApi, type Row} from '../api';
import {time} from '../common';
import {providerReady} from './aiProviders';
import './AIReview.css';

type ReviewSettings = {enabled:boolean;provider_id:string;platform_policy:'hold'|'warn';revision:number;english_only:true};
type ReviewData = {settings:ReviewSettings;providers:Row[];status:'disabled'|'ready'|'paused';reason?:string;rules:Row[];records:Row[];rule_version?:string};
type EditableSettings = Pick<ReviewSettings,'enabled'|'provider_id'|'platform_policy'>;
const edited=(settings:ReviewSettings):EditableSettings=>({enabled:settings.enabled,provider_id:settings.provider_id||'',platform_policy:settings.platform_policy||'hold'});
const sameSettings=(a:EditableSettings,b:EditableSettings)=>a.enabled===b.enabled&&a.provider_id===b.provider_id&&a.platform_policy===b.platform_policy;
const actions:Record<string,string>={post:'发帖',reply:'回复',repost:'转发',like:'点赞',follow:'关注',unlike:'取消点赞',unfollow:'取消关注',delete_post:'删除帖子',skip:'跳过'};
const outcomes:Record<string,string>={pass:'通过',passed:'通过',allowed:'通过',approved:'通过',block:'需修改',blocked:'需修改',fail:'不通过',failed:'检查失败',error:'检查失败',needs_human:'待人工',needs_review:'待人工',unknown:'待确认',pending:'待检查',checking:'检查中',reviewing:'检查中',queued:'等待模型额度',running:'审核中',disabled:'未启用',skipped:'未进行',not_required:'无需检查',not_applicable:'不涉及',stale:'已失效',invalidated:'已失效',violation:'命中',pass_with_platform_warning:'平台风险提醒',warning:'平台风险',warn:'平台风险',non_english:'语言不符',english:'符合',compliant:'符合',noncompliant:'语言不符'};
const executionStates:Record<string,string>={pending:'待执行',draft:'待人工确认',published:'已完成',sending:'正在执行',uncertain:'待核对',failed:'执行失败',blocked:'草稿已保留',needs_human:'待人工确认',discarded:'已放弃',skipped:'已跳过',held:'草稿已保留',paused:'暂停执行'};
const tiers:Record<string,string>={competition:'主赛禁令',hard:'主赛禁令',platform:'平台风险',local:'本队要求',team:'本队要求',language:'本队要求',interface:'接口限制',unknown:'待核实'};
function available(provider?:Row){if(!provider)return false;if(typeof provider.ready==='boolean')return provider.ready&&provider.enabled!==false;if(typeof provider.available==='boolean')return provider.available&&provider.enabled!==false;return providerReady(provider as any);}
function outcome(value:unknown,fallback='待检查'){const key=typeof value==='object'&&value!==null?String((value as Row).status||(value as Row).decision||''):String(value||'');return outcomes[key]||key||fallback;}
export function reviewLanguageLabel(language?:Row){return language?.status==='pass'?'英文符合':language?.status==='blocked'?'语言不符':outcome(language);}
export function reviewClauseLabel(record:Row){const findings=rows(record.findings).filter(item=>/^[CP]\d/.test(item.rule_id||''));if(record.status==='stale')return '原审核已失效';if(record.status==='error')return '检查未完成';if(record.status==='running')return '审核中';if(record.status==='queued')return '等待模型额度';if(findings.some(item=>item.status==='violation'&&item.rule_id.startsWith('C')))return '主赛禁令命中';if(findings.some(item=>item.status==='violation'))return '平台风险';if(findings.some(item=>item.status==='unknown'))return '待人工';if(!findings.length)return record.review_kind==='deterministic'?'未进行 AI 条款复核':'尚无有效条款结论';return '通过';}
function sourceUrl(value:unknown){try{const url=new URL(String(value));return ['http:','https:'].includes(url.protocol)?url.href:undefined;}catch{return undefined;}}
function rows(value:unknown):Row[]{return Array.isArray(value)?value.map(item=>typeof item==='string'?{text:item}:item).filter(Boolean):[];}
function at(value:unknown){return time(typeof value==='number'?new Date(value*1000).toISOString():value);}
function Evidence({items}:{items:unknown}){
 const evidence=rows(items);
 return evidence.length?<ul className="ai-review-evidence">{evidence.map((item,index)=><li key={index}>{(item.rule_id||item.section)&&<strong>{[item.rule_id,item.section].filter(Boolean).join(' · ')} </strong>}{item.field&&<small>{({body:'自写正文',repost:'转载正文',image_text:'图片文字'} as Record<string,string>)[item.field]||item.field}{Number.isInteger(item.start)&&Number.isInteger(item.end)?` · 字符 ${item.start}–${item.end}`:''}</small>}{(item.quote||item.excerpt||item.text||item.fragment)&&<blockquote>{item.quote||item.excerpt||item.text||item.fragment}</blockquote>}{item.reason&&<p>{item.reason}</p>}{sourceUrl(item.source_url||item.url)&&<a href={sourceUrl(item.source_url||item.url)} target="_blank" rel="noreferrer">{item.source_title||'查看条款来源'} ↗</a>}</li>)}</ul>:<p className="muted">未记录命中片段。</p>;
}
export function ReviewRecordDetails({record,rules=[]}:{record:Row;rules?:Row[]}){
 const review=record.review||record.result||record,language=review.language||record.language||{},findings=review.findings||review.evidence||record.evidence;
 const missing=review.missing_context||review.missing_evidence||record.missing_context||[];
 return <div className="ai-review-record-detail stack">
  {(record.body_preview||record.body||review.body)&&<div><strong>本次检查正文摘要</strong><blockquote>{record.body_preview||record.body||review.body}</blockquote></div>}
  <dl className="ai-review-facts"><div><dt>语言检查 · 本队要求</dt><dd>{reviewLanguageLabel(language)}{language.reason&&` · ${language.reason}`}</dd></div><div><dt>条款结论</dt><dd>{reviewClauseLabel(review)}{review.summary&&` · ${review.summary}`}</dd></div><div><dt>实际执行状态</dt><dd>{executionStates[record.execution_state||record.draft_status]||record.execution_state||record.draft_status||'未记录'}</dd></div><div><dt>审核连接</dt><dd>{record.provider_name||review.provider_name||record.provider_id||review.provider_id||'本地检查'}{(record.model||review.model)&&` · ${record.model||review.model}`}</dd></div><div><dt>规则 / 语言策略版本</dt><dd>{record.rules_version||review.rules_version||record.rule_version||'未记录'} / {language.policy_version||record.language_policy_version||'未记录'}</dd></div><div><dt>检查时间</dt><dd>{at(record.created_at||record.checked_at||review.checked_at)}</dd></div></dl>
  <section><strong>语言证据</strong><Evidence items={language.evidence||language.fragments}/></section>
  <section><strong>条款依据</strong>{rows(findings).length?rows(findings).map((finding,index)=>{const rule=rules.find(item=>item.id===finding.rule_id||item.rule_id===finding.rule_id);return <div key={index}><h4>{finding.rule_id} {rule?.title} · {outcome(finding.status)}</h4><p>{finding.reason}</p><Evidence items={finding.evidence}/>{rows(finding.missing).length>0&&<p>缺失资料：{rows(finding.missing).map(item=>item.text||item.reason).join('、')}</p>}{sourceUrl(rule?.source_url)&&<a href={sourceUrl(rule?.source_url)} target="_blank" rel="noreferrer">{rule?.section||'查看条款来源'} ↗</a>}</div>}):<p className="muted">尚无有效条款依据。</p>}</section>
  <p className="muted">缺失资料：{Array.isArray(missing)?missing.map(item=>typeof item==='string'?item:item.reason||item.name||JSON.stringify(item)).join('、')||'未记录缺失项':String(missing||'未记录缺失项')}</p>
  {(review.error||record.error)&&<Alert type="error" showIcon title="检查未完成" description={review.error||record.error}/>}
  {record.agent_id&&record.draft_id&&<Link to={`/ai/agents/${encodeURIComponent(record.agent_id)}?tab=drafts&draft=${encodeURIComponent(record.draft_id)}`}>前往草稿查看或重新提交 ↗</Link>}
 </div>;
}

export default function AIReview(){
 const query=useApi<ReviewData>('/agents/review'),save=useAction<ReviewData>('/agents/review'),client=useQueryClient();
 const [baseline,setBaseline]=useState<ReviewSettings|null>(null),[form,setForm]=useState<EditableSettings|null>(null),[error,setError]=useState(''),[saved,setSaved]=useState(false);
 const data=query.data;
 useEffect(()=>{
  if(data?.settings&&(!form||!baseline||sameSettings(form,edited(baseline)))){setBaseline(data.settings);setForm(edited(data.settings));}
 },[data?.settings]);
 const dirty=!!form&&!!baseline&&!sameSettings(form,edited(baseline));
 const providers=data?.providers||[],selected=providers.find(provider=>provider.id===form?.provider_id),effectiveProvider=providers.find(provider=>provider.id===data?.settings.provider_id);
 const invalid=!!form&&form.enabled&&!available(selected),conflict=!!data&&!!baseline&&data.settings.revision!==baseline.revision;
 function change(values:Partial<EditableSettings>){setForm(current=>current?{...current,...values}:current);setError('');setSaved(false);}
 async function persist(){
  if(!form||!baseline||invalid)return;
  setError('');setSaved(false);
  try{
   const result=await save.mutateAsync({...form,expected_revision:baseline.revision});
   await client.cancelQueries({queryKey:['/agents/review']});
   client.setQueryData(['/agents/review',{}],result);setBaseline(result.settings);setForm(edited(result.settings));setSaved(true);
   void client.invalidateQueries({queryKey:['/agents/review']});
  }catch(cause){setError(cause instanceof Error?cause.message:'保存失败，请重试');void query.refetch();}
 }
 const enabled=!!data?.settings.enabled,status=enabled?(data?.status==='paused'||!available(effectiveProvider)?'paused':'ready'):'disabled';
 return <div className="ai-review-page stack">
  <div className="ai-review-heading"><h2>AI 审核</h2>{data&&<Tag color={status==='ready'?'green':status==='paused'?'orange':'default'}>{status==='ready'?'当前生效：已开启':status==='paused'?'已开启 · 暂停审核':'当前生效：已关闭'}</Tag>}</div>
  {query.isError&&<Alert type="error" showIcon title="审核设置读取失败" description={query.error.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>}
  {query.isPending?<Skeleton active paragraph={{rows:5}}/>:data&&form&&baseline&&<>
   <section className="panel ai-review-settings" aria-label="审核设置">
    <div className="ai-review-control"><div><h3>比赛条款 AI 审核</h3><p className="muted">开启后增加 AI 条款与语言复核；关闭后跳过全部 AI 审核，仅保留不调用 AI 的本地英文检查。</p></div><Space><span>{form.enabled?'开启':'关闭'}</span><Switch aria-label="比赛条款 AI 审核" checked={form.enabled} disabled={save.isPending||query.isError} onChange={value=>change({enabled:value})}/></Space></div>
    {status==='paused'&&<Alert type="warning" showIcon title="审核连接不可用，相关外发已暂停" description={data.reason||'保留草稿；角色仍可阅读、生成和查看历史。可恢复连接继续审核，或关闭 AI 审核，保留本地英文检查并按原有流程继续。'}/>}
    <div className="ai-review-fields"><div><label htmlFor="review-provider">审核 / 语言判定连接</label><Select id="review-provider" aria-label="审核 / 语言判定连接" value={form.provider_id} disabled={save.isPending||query.isError} onChange={value=>change({provider_id:value})} options={[{value:'',label:'请选择文字 AI 连接'},...providers.map(provider=>({value:provider.id,label:`${provider.name}${provider.model?` · ${provider.model}`:''}${available(provider)?'':' · 不可用'}`,disabled:!available(provider)})),...(form.provider_id&&!selected?[{value:form.provider_id,label:'原连接已失效',disabled:true}]:[])]}/><p className="muted">复用 <Link to="/ai/settings">AI 设置</Link> 中的文字连接，必要时用于语言判断。</p></div><div><label htmlFor="review-platform-policy">平台规则风险</label><Select id="review-platform-policy" aria-label="平台规则风险" value={form.platform_policy} disabled={save.isPending||query.isError} onChange={value=>change({platform_policy:value})} options={[{value:'hold',label:'保留草稿，转人工复核（推荐）'},{value:'warn',label:'仅提醒，允许继续执行'}]}/><p className="muted">仅在条款 AI 审核开启时影响平台风险，不能覆盖主赛禁令。</p></div></div>
    {invalid&&<Alert type="warning" showIcon title="请先选择可用的文字 AI 连接，再保存开启。"/>}
    {!enabled&&<Alert type="info" showIcon title="当前已跳过全部 AI 审核" description="不调用审核模型，不要求配置审核连接。本地英文检查仍开启；无法本地确定的语言不会触发模型或额外挂起。原有执行规则继续生效。"/>}
    {conflict&&<Alert type="warning" showIcon title="审核设置已由其他窗口更新，当前编辑已保留" description="保存前请载入最新版本号并核对当前选项。" action={<Button disabled={save.isPending} onClick={()=>{setBaseline(data.settings);setError('');}}>载入最新版本号</Button>}/>}
    {error&&<Alert type="error" showIcon title="审核设置未保存" description={error}/>}
    {saved&&!dirty&&<p role="status" className="ai-review-success">审核设置已保存并生效。</p>}
    <div className="ai-review-save"><p className="muted">保存后生效。关闭后跳过全部 AI 审核，仅因 AI 审核挂起的草稿经本地英文检查后按角色原有模式继续；原有权限、限流与人工审阅要求继续生效。</p><Space wrap>{dirty&&<Tag color="orange">有未保存更改</Tag>}<Button type="primary" loading={save.isPending} disabled={!dirty||invalid||conflict||query.isError} onClick={()=>void persist()}>保存审核设置</Button></Space></div>
    <div className="ai-review-flow"><span>生成草稿</span><span aria-hidden="true">→</span><strong>{enabled?'英文检查 + AI 条款审核':'本地英文检查 · AI 审核已跳过'}</strong><span aria-hidden="true">→</span><span>按角色模式执行</span></div>
   </section>
   <section className="panel ai-review-language" aria-label="发布语言检查"><div className="ai-review-control"><h3>发布语言 <strong>English only</strong></h3><Tag>本地检查 · 始终开启</Tag></div><p>本地英文检查始终开启，不调用 AI；表情、颜文字、特殊符号正常允许，拦截正文中可明确识别的中文等非英文文字。</p><div className="ai-review-language-samples"><span>✓ English + 🙂</span><span>✓ 单独表情 👏🎉</span><span>{'✓ 颜文字 ¯\\_(ツ)_/¯'}</span><span>✓ 特殊符号 → ∞ ≠</span><span>需修改：夹杂中文等可明确识别的非英文文字</span></div><p className="muted">开启审核后结合模型进一步确认语言与条款。关闭时，无法本地确定的拉丁文字不调用模型、不额外挂起；图片文字也不做 AI 识别。人设与后台理由可继续用中文编辑。</p></section>
   <section className="panel" aria-label="审核记录与规则依据"><Tabs items={[{key:'records',label:'审核记录',children:<><p className="muted">显示最近 100 条审核记录。语言检查、条款结论和实际执行状态分别展示；通过审核不代表已经发布。</p><Table<Row> size="middle" rowKey={(record,index)=>String(record.id||record.review_id||index)} dataSource={data.records||[]} scroll={{x:800}} locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无审核记录。相关操作提交后会显示真实检查结果。"/>}} pagination={{pageSize:10,showSizeChanger:false}} expandable={{expandIcon:({expanded,onExpand,record})=><Button type="link" size="small" aria-label={expanded?'收起审核依据':'查看审核依据'} onClick={event=>{event.stopPropagation();onExpand(record,event);}}>{expanded?'收起':'查看依据'}</Button>,expandedRowRender:record=><ReviewRecordDetails record={record} rules={data.rules}/>,expandRowByClick:true,columnTitle:'依据'}} columns={[{title:'角色 / 动作',key:'agent',render:(_,record)=><><strong>{record.agent_name||record.agent_id||'未记录角色'}</strong><small className="ai-review-table-small">{actions[record.action]||record.action||'未记录动作'}</small></>},{title:'正文 / 检查摘要',key:'body',render:(_,record)=><span className="ai-review-excerpt">{record.body_preview||record.body||record.summary||record.review?.reason||record.reason||'无自写正文'}</span>},{title:'语言检查',key:'language',render:(_,record)=>reviewLanguageLabel(record.language||record.review?.language||record.result?.language)},{title:'条款结论',key:'review',render:(_,record)=>reviewClauseLabel(record.review||record.result||record)},{title:'实际执行状态',key:'execution',render:(_,record)=>executionStates[record.execution_state||record.draft_status]||record.execution_state||record.draft_status||'未记录'},{title:'检查时间',key:'time',render:(_,record)=>at(record.created_at||record.checked_at)}]}/></>},{key:'rules',label:'规则依据',children:!data.rules?.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="规则清单暂不可用，请重试读取。"/>:<><p className="muted">规则版本：{data.rule_version||'未记录'}。审核结论是本地检查结果，最终解释由赛事与平台决定。</p><Collapse items={data.rules.map((rule,index)=>({key:String(rule.id||rule.rule_id||index),label:<Space wrap><Tag>{tiers[rule.layer||rule.tier||rule.category]||rule.layer||rule.tier||rule.category||'条款'}</Tag><strong>{rule.id||rule.rule_id} · {rule.title||rule.name}</strong></Space>,children:<div className="stack"><p>{rule.summary||rule.description||rule.text}</p>{rule.conditions&&<p>适用条件：{Array.isArray(rule.conditions)?rule.conditions.join('；'):rule.conditions}</p>}{rule.exceptions&&<p>边界与例外：{Array.isArray(rule.exceptions)?rule.exceptions.join('；'):rule.exceptions}</p>}{rule.section&&<p>条款：{rule.section}</p>}{sourceUrl(rule.source_url||rule.url)&&<a target="_blank" rel="noreferrer" href={sourceUrl(rule.source_url||rule.url)}>{rule.source_title||'查看官方来源'} ↗</a>}{rows(rule.sources).map((source,i)=><p key={i}>{sourceUrl(source.url)&&<a target="_blank" rel="noreferrer" href={sourceUrl(source.url)}>{source.title||source.section||'查看条款来源'} ↗</a>}</p>)}</div>}))}/></>}]} /></section>
   <p className="muted ai-review-footnote">修改正文、目标、账号或规则版本后，原审核结果失效；权限、限流和原有人工审阅模式继续生效。</p>
  </>}
 </div>;
}

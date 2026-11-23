import {useEffect, useState} from 'react';
import {Link} from 'react-router-dom';
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {Alert, Button, Collapse, Empty, Form, Input, Pagination, Select, Space, Table, Tag, Typography} from 'antd';
import {request, useApi, type Row} from '../api';
import {time} from '../common';
import {useAIJob} from '../useAIJob';
import AIJobStatus from '../AIJobStatus';
import {providerReady, type Settings} from './aiProviders';
import ResearchAutomation from './ResearchAutomation';
import {referenceFailureDetails} from './researchReferenceFailure';
import './contentResearch.css';

type ResearchSource = {id:string;title?:string;text?:string;post_id?:number;kind?:string;group?:string;url?:string};
type ResearchBrief = {
 brief_id:string;generated_at:string;expires_at:string;provider_id:string;model:string;
 scope:Row;instruction:string;report:string;sources:ResearchSource[];summary:Row|string|null;usage:Row|null;kind?:'concern_radar'|'popular';
 citation_status:'valid'|'invalid';citation_errors:string[];active:boolean;expired:boolean;can_activate:boolean;sources_available:boolean;
};
type ResearchReference = {
 agent_id:string|number;brief_id:string;provided_at:string;status:'provided';call_outcome?:'succeeded'|'failed';draft_id?:string|number;
 error?:string;error_code?:string;diagnostics?:Record<string,unknown>;
};
export type ResearchState = {
 kind:'content_research';revision:number;active_brief_id:string|null;briefs:ResearchBrief[];
 references:ResearchReference[];created_brief_id?:string;
};
const researchKey=['/ai/research'] as const;
const isObject=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const validTime=(value:unknown):value is string=>typeof value==='string'&&Number.isFinite(Date.parse(value));
export function parseResearchState(value:unknown):ResearchState {
 if(!isObject(value)||value.kind!=='content_research'||!Number.isInteger(value.revision)||Number(value.revision)<0
  ||!(value.active_brief_id===null||typeof value.active_brief_id==='string')||!Array.isArray(value.briefs)||!Array.isArray(value.references))
  throw new Error('内容研究数据格式不正确，请刷新后重试。');
 for(const brief of value.briefs){
  if(!isObject(brief)||typeof brief.brief_id!=='string'||!brief.brief_id||!validTime(brief.generated_at)||!validTime(brief.expires_at)
   ||typeof brief.report!=='string'||typeof brief.provider_id!=='string'||typeof brief.model!=='string'||!isObject(brief.scope)
   ||typeof brief.instruction!=='string'||!Array.isArray(brief.sources)||!brief.sources.every(source=>isObject(source)&&typeof source.id==='string'
    &&(source.title===undefined||typeof source.title==='string')&&(source.text===undefined||typeof source.text==='string')
    &&(source.url===undefined||typeof source.url==='string')&&(source.post_id===undefined||typeof source.post_id==='number'))
   ||!['valid','invalid'].includes(String(brief.citation_status))||!Array.isArray(brief.citation_errors)||!brief.citation_errors.every(error=>typeof error==='string')
   ||typeof brief.active!=='boolean'||typeof brief.expired!=='boolean'||typeof brief.can_activate!=='boolean'||typeof brief.sources_available!=='boolean')
   throw new Error('研究报告字段不完整，请刷新后重试。');
 }
 for(const reference of value.references){
  if(!isObject(reference)||!['string','number'].includes(typeof reference.agent_id)||typeof reference.brief_id!=='string'
   ||!validTime(reference.provided_at)||reference.status!=='provided'
   ||(reference.call_outcome!==undefined&&!['succeeded','failed'].includes(String(reference.call_outcome)))
   ||(reference.error!==undefined&&typeof reference.error!=='string')
   ||(reference.error_code!==undefined&&typeof reference.error_code!=='string')
   ||(reference.diagnostics!==undefined&&!isObject(reference.diagnostics)))
   throw new Error('参考提供记录格式不正确，请刷新后重试。');
 }
 return value as unknown as ResearchState;
}
function researchScope(params:Row):Row {
 return Object.fromEntries(['author_id','own','since','until'].filter(key=>params[key]!==undefined&&params[key]!==null&&params[key]!=='').map(key=>[key,params[key]]));
}
function scopeLabel(scope:Row):string {
 const parts:string[]=[];
 if(scope.author_id!==undefined)parts.push(`作者 ${scope.author_id}`);
 if(scope.own)parts.push(({own:'本队账号',other:'其他账号',others:'其他账号',all:'全部账号'} as Record<string,string>)[String(scope.own)]||String(scope.own));
 if(scope.since)parts.push(`自 ${scope.since}`);
 if(scope.until)parts.push(`至 ${scope.until}`);
 return parts.join(' · ')||'全部本地资料';
}
function ResearchSources({sources}:{sources:ResearchSource[]}) {
 const [page,setPage]=useState(1),pageSize=8;
 useEffect(()=>setPage(1),[sources]);
 if(!sources.length)return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此报告没有引用资料"/>;
 return <div className="research-sources">
  {sources.slice((page-1)*pageSize,page*pageSize).map((source,index)=><article key={`${source.id}-${index}`}>
   <Space wrap><Tag>{source.id}</Tag><strong>{source.title||'引用资料'}</strong>{source.post_id&&<Link to={`/posts?post=${source.post_id}`}>原帖 #{source.post_id} ↗</Link>}{source.url&&/^https?:\/\//i.test(source.url)&&<a href={source.url} target="_blank" rel="noreferrer">来源 ↗</a>}</Space>
   <p className="research-prose">{source.text||'此资料没有正文'}</p>
  </article>)}
  {sources.length>pageSize&&<Pagination size="small" current={page} pageSize={pageSize} total={sources.length} showSizeChanger={false} onChange={setPage}/>}
 </div>;
}
async function researchAction(path:string,payload:Row):Promise<ResearchState> {
 const bootstrap=await request<{csrf_token:string}>('/bootstrap');
 return parseResearchState(await request(path,{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':bootstrap.csrf_token},body:JSON.stringify(payload)}));
}

export default function ContentResearch({params}:{params:Row}) {
 const client=useQueryClient(),providers=useApi<Settings>('/ai/providers');
 const research=useQuery<ResearchState,Error>({queryKey:researchKey,queryFn:async({signal})=>parseResearchState(await request('/ai/research',{signal})),refetchInterval:15000,retry:false});
 const [providerId,setProviderId]=useState<string|null>(null),[instruction,setInstruction]=useState(''),[selectedBriefId,setSelectedBriefId]=useState<string|null>(null),[actionError,setActionError]=useState(''),[notice,setNotice]=useState(''),[now,setNow]=useState(Date.now());
 const settings=providers.data,allProviders=Array.isArray(settings?.providers)?settings.providers:[],textProviders=allProviders.filter(provider=>provider.protocol==='openai_chat');
 const preferred=providerId??settings?.default_provider_id??textProviders.find(provider=>provider.is_default)?.id;
 const selectedProviderId=providerId===''?'':textProviders.some(provider=>provider.id===preferred)?preferred:textProviders.find(providerReady)?.id;
 const selectedProvider=textProviders.find(provider=>provider.id===selectedProviderId);
 const state=research.data,briefs=state?.briefs||[],selectedBrief=briefs.find(brief=>brief.brief_id===selectedBriefId)||briefs.find(brief=>brief.brief_id===state?.active_brief_id)||briefs[0];
 const scope=researchScope(params),expired=!!selectedBrief&&(selectedBrief.expired||Date.parse(selectedBrief.expires_at)<=now),active=!!selectedBrief&&selectedBrief.brief_id===state?.active_brief_id;
 const activeBrief=briefs.find(brief=>brief.brief_id===state?.active_brief_id),activeExpired=!!activeBrief&&(activeBrief.expired||Date.parse(activeBrief.expires_at)<=now);
 useEffect(()=>{const timer=window.setInterval(()=>setNow(Date.now()),30000);return()=>window.clearInterval(timer);},[]);
 async function accept(data:ResearchState) {
  await client.cancelQueries({queryKey:researchKey});
  client.setQueryData(researchKey,data);
  await client.invalidateQueries({queryKey:researchKey});
 }
 const run=useAIJob<ResearchState>({path:'/ai/research/run',kind:'research',onRecovered:accept});
 const activation=useMutation<ResearchState,Error,Row>({mutationFn:payload=>researchAction('/ai/research/active',payload),onSuccess:accept});
 const busy=run.isPending||activation.isPending;
 async function generate(){
  if(busy||!providerReady(selectedProvider))return;
  setActionError('');setNotice('');
  try{
   const data=await run.mutateAsync({provider_id:selectedProviderId,params:scope,mode:'popular',instruction:instruction.trim(),sample_size:100,include_news:true});
   await accept(data);
   setSelectedBriefId(data.created_brief_id??data.briefs[0]?.brief_id??null);
   setNotice('研究报告已保存。需要你明确启用后，才会供发文 Agent 参考。');
  }catch(error){setActionError(error instanceof Error?error.message:'研究失败，已有报告仍然保留。');}
 }
 async function setReference(briefId:string|null){
  if(busy||!state)return;
  setActionError('');setNotice('');
  try{
   await activation.mutateAsync({brief_id:briefId,expected_revision:state.revision});
   setNotice(briefId?'已设为发文 Agent 的参考；参考提供记录会在实际生成时出现。':'已停用参考；已保存的研究报告仍可查看。');
  }catch(error){setActionError(`${error instanceof Error?error.message:'参考设置失败'} 请刷新报告后再试。`);}
 }
 return <div className="content-research stack">
  <ResearchAutomation params={params}/>
  <div className="ai-section-head"><div><Space wrap><h3>内容研究报告</h3><Tag>支持手动补充</Tag></Space><p className="research-help">研究受关注帖子的特征与反例，保存报告并核对依据。打开本页读取报告，后台按已保存的自动设置运行。</p></div><Button disabled={busy} loading={research.isFetching} onClick={()=>void research.refetch()}>刷新报告</Button></div>
  {providers.isError&&<Alert type="error" title="研究服务配置读取失败" description={providers.error.message}/>}
  {research.isError&&<Alert type="error" title="研究报告读取失败" description={research.error.message}/>}
  {actionError&&<Alert type="error" title={actionError} showIcon/>}
  {notice&&<Alert type="success" title={notice} showIcon/>}
  <AIJobStatus job={run} label="内容研究"/>
  <section className="research-composer">
   <Form layout="vertical">
    <Form.Item label="研究文字 AI 服务"><div className="research-provider"><Select aria-label="研究文字 AI 服务" value={selectedProviderId??''} disabled={busy} onChange={setProviderId} options={[{value:'',label:'请选择已配置的文字服务'},...textProviders.map(provider=>({value:provider.id,label:`${provider.name} · ${provider.model}${providerReady(provider)?'':'（不可用）'}`,disabled:!providerReady(provider)}))]}/><Link to="/ai/settings">配置服务</Link></div></Form.Item>
    <Form.Item label="研究重点（可选）"><Input.TextArea aria-label="内容研究重点" value={instruction} disabled={busy} onChange={event=>setInstruction(event.target.value)} maxLength={12000} autoSize={{minRows:2,maxRows:6}} placeholder="例如：比较开头方式、长度、提问和回复策略；同时找出不适用的场景。"/></Form.Item>
   </Form>
   <p className="research-scope">本次资料范围：{scopeLabel(scope)} · 最多 100 帖，补充新闻依据</p>
   <div className="research-run"><Button type="primary" loading={run.isPending} disabled={busy||!providerReady(selectedProvider)||research.isError} onClick={()=>void generate()}>开始研究并保存</Button><span>点击后调用所选文字 AI；生成报告不会自动启用参考。</span></div>
  </section>
  {research.isPending?<p role="status">正在读取已保存的研究报告…</p>:!briefs.length&&!research.isError?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未生成研究报告"/>:null}
  {!!briefs.length&&<section className="research-report-section stack">
   <div className="research-report-picker"><label htmlFor="research-report-selection">已保存报告</label><Select id="research-report-selection" aria-label="已保存报告" value={selectedBrief?.brief_id} onChange={setSelectedBriefId} options={briefs.map(brief=>({value:brief.brief_id,label:`${time(brief.generated_at)} · ${brief.model}${brief.brief_id===state?.active_brief_id?' · 当前参考':''}`}))}/></div>
   {activeBrief&&<p className="research-current">当前参考：{time(activeBrief.generated_at)} · {activeBrief.model}{activeExpired?' · 已到期，不再提供给 Agent':''}</p>}
   {selectedBrief&&<article className="research-report">
    <div className="ai-section-head"><Space wrap><h3>研究报告</h3>{selectedBrief.kind==='concern_radar'&&<Tag color="blue">顾虑雷达</Tag>}<Tag color={expired?'default':active?'green':'default'}>{expired?'已到期':active?'正在供参考':'未启用参考'}</Tag><Tag>{selectedBrief.model}</Tag></Space><Typography.Text copyable={{text:selectedBrief.report}}>复制报告</Typography.Text></div>
    <div className="research-report-meta"><span>生成 {time(selectedBrief.generated_at)}</span><span>到期 {time(selectedBrief.expires_at)}</span><span>资料范围：{scopeLabel(selectedBrief.scope)}</span></div>
    {selectedBrief.citation_status==='invalid'&&<Alert type="warning" title="引用校验未通过，不能供 Agent 参考" description={selectedBrief.citation_errors.join('；')||'请核对资料后重新生成研究。'}/>}
    {!selectedBrief.sources_available&&<Alert type="warning" title="来源已不可用，不能供发文 Agent 参考。" description="已保存的报告仍可查看；请重新采集资料后生成研究。"/>}
    {expired&&<Alert type="info" title="此报告已到期，仅保留查看，不再供发文 Agent 参考。"/>}
    <div className="research-prose research-report-body">{selectedBrief.report}</div>
    <Collapse items={[{key:'sources',label:`核对引用资料（${selectedBrief.sources.length} 条）`,children:<ResearchSources sources={selectedBrief.sources}/>}]} />
    <div className="research-reference-action"><Button disabled={busy||(!active&&(!selectedBrief.can_activate||expired||!selectedBrief.sources_available||selectedBrief.citation_status!=='valid'))} loading={activation.isPending} type={active?'default':'primary'} onClick={()=>void setReference(active?null:selectedBrief.brief_id)}>{active?'停用参考':'供发文 Agent 参考'}</Button><span>{active?'发文 Agent 可读取这份报告作为建议，仍需自行决定如何表达。':'启用后替换当前参考；不会改动角色设定或立即发布帖子。'}</span></div>
   </article>}
  </section>}
  <section className="research-references"><h3>参考提供记录</h3><p className="research-help">记录报告何时提供给发文 Agent；不代表 Agent 已采用建议，也不代表成绩改善。</p>
   <Table size="small" rowKey={(row:ResearchReference)=>`${row.agent_id}-${row.brief_id}-${row.provided_at}-${row.draft_id??''}`} dataSource={state?.references||[]} pagination={{pageSize:8,hideOnSinglePage:true}} scroll={{x:1000}} locale={{emptyText:'尚无参考提供记录'}} columns={[
    {title:'Agent',dataIndex:'agent_id',render:(value:string|number)=><Link to={`/ai/agents/${value}`}>{String(value)}</Link>},
    {title:'提供时间',dataIndex:'provided_at',render:time},
    {title:'研究报告',dataIndex:'brief_id',render:(id:string)=>{const brief=briefs.find(item=>item.brief_id===id);return brief?time(brief.generated_at):'历史报告';}},
    {title:'提供状态',render:()=> <Tag>已提供参考</Tag>},
    {title:'生成调用',dataIndex:'call_outcome',render:(value?:string)=>value==='succeeded'?'调用成功':value==='failed'?'调用失败':'结果待记录'},
    {title:'失败原因',width:300,render:(_:unknown,row:ResearchReference)=>{
     if(row.call_outcome!=='failed')return '—';
     const details=referenceFailureDetails(row);
     return <div className="research-prose">{row.error_code==='output_truncated'&&<Tag color="orange">输出被截断</Tag>}{row.error_code==='reasoning_only'&&<Tag color="orange">只返回推理、没有正文</Tag>}{details.message}{details.notice&&<div className="muted">{details.notice}</div>}</div>;
    }}
   ]}/>
  </section>
 </div>;
}

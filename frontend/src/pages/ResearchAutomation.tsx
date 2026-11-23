import {useEffect, useState} from 'react';
import {Link} from 'react-router-dom';
import {useMutation, useQueryClient} from '@tanstack/react-query';
import {Alert, Button, Collapse, Form, Input, InputNumber, Select, Space, Switch, Tag} from 'antd';
import {request, useApi, type Row} from '../api';
import {time} from '../common';
import {providerReady, type Settings} from './aiProviders';

export type AutomationSettings = {
 revision:number; enabled:boolean; provider_id:string; interval_seconds:number; auto_activate:boolean;
 params:Row; instruction:string; sample_size:number; include_news:boolean; max_output_tokens:number|null;
 mode:'radar'|'popular';
};
export type AutomationState = {
 settings:AutomationSettings;
 status:{running:boolean;run_id?:string|null;lease_healthy?:boolean;stale?:boolean;interrupted?:boolean;interrupted_run_id?:string|null;cancel_requested?:boolean;progress?:{stage?:string;started_at?:string;elapsed_seconds?:number;received_bytes?:number}|null;last_run_at?:string|null;last_success_at?:string|null;next_run_at?:string|null;last_error?:string|null;last_result?:{outcome:string;run_id?:string;brief_id?:string;activated?:boolean;activation_note?:string}|null};
};
const endpoint='/ai/research/automation';
const queryKey=[endpoint,{}] as const;
const stageLabels:Record<string,string>={preparing:'正在整理资料',request:'正在发送请求',dns:'正在连接 AI 服务',connect:'正在连接 AI 服务',tls:'正在连接 AI 服务',send:'正在发送请求',headers:'等待模型开始回答',body:'正在接收回答',thinking:'模型正在思考',receiving:'正在生成回答',complete:'回答已完成，正在保存'};
function elapsedText(progress:{started_at?:string;elapsed_seconds?:number}){
 const seconds=progress.started_at?Math.max(0,(Date.now()-Date.parse(progress.started_at))/1000):progress.elapsed_seconds??0;
 const minutes=Math.floor(seconds/60);return minutes?`${minutes} 分 ${Math.floor(seconds%60)} 秒`:`${Math.floor(seconds)} 秒`;
}
const resultLabels:Record<string,string>={
 generated:'报告已保存',activated:'报告已保存并供 Agent 参考',
 unchanged:'资料未变化，已跳过',skipped:'本轮已跳过',empty:'暂无可分析资料',
 failed:'本轮分析失败',cancelled:'本轮已取消',interrupted:'上次分析已中断',retry_pending:'已安排重新分析',
};
function scopeText(scope:Row){
 return [scope.own==='own'?'本队帖子':scope.own==='unknown'?'归属未知的帖子':'全部帖子',
  scope.author_id?`作者 #${scope.author_id}`:'',scope.since?`从 ${time(scope.since)}`:'',scope.until?`至 ${time(scope.until)}`:''].filter(Boolean).join(' · ');
}
const scopeFrom=(params:Row)=>Object.fromEntries(['own','author_id','since','until'].filter(key=>params[key]!==undefined&&params[key]!==null&&params[key]!=='').map(key=>[key,params[key]]));

export default function ResearchAutomation({params}:{params:Row}){
 const [fast,setFast]=useState(false);
 const query=useApi<AutomationState>(endpoint,{},fast?3000:10000),providers=useApi<Settings>('/ai/providers'),client=useQueryClient();
 const [draft,setDraft]=useState<AutomationSettings|null>(null),[dirty,setDirty]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState(false);
 useEffect(()=>{if(query.data?.settings&&!dirty)setDraft(query.data.settings);},[query.data,dirty]);
 useEffect(()=>{setFast(!!(query.data?.status?.running||query.data?.status?.stale||query.data?.status?.last_result?.outcome==='retry_pending'));},[query.data?.status?.running,query.data?.status?.stale,query.data?.status?.last_result?.outcome]);
 const save=useMutation<AutomationState,Error,AutomationSettings>({mutationFn:async settings=>{
  const bootstrap=await request<{csrf_token:string}>('/bootstrap');
  const {revision,...values}=settings;
  return request<AutomationState>(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':bootstrap.csrf_token},body:JSON.stringify({...values,expected_revision:revision})});
 },onSuccess:async state=>{
  await client.cancelQueries({queryKey});
  client.setQueryData(queryKey,state);
  setDraft(state.settings);setDirty(false);setSaved(true);
 }});
 const cancel=useMutation<AutomationState,Error,string>({mutationFn:async run_id=>{
  const bootstrap=await request<{csrf_token:string}>('/bootstrap');
  return request<AutomationState>(endpoint+'/cancel',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':bootstrap.csrf_token},body:JSON.stringify({run_id})});
 },onSuccess:async state=>{client.setQueryData(queryKey,state);await client.invalidateQueries({queryKey});},onError:failure=>setError(failure.message)});
 const retry=useMutation<AutomationState,Error,string>({mutationFn:async run_id=>{
  setError('');
  const bootstrap=await request<{csrf_token:string}>('/bootstrap');
  return request<AutomationState>(endpoint+'/retry',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':bootstrap.csrf_token},body:JSON.stringify({run_id})});
 },onSuccess:async state=>{client.setQueryData(queryKey,state);await client.invalidateQueries({queryKey});},onError:failure=>setError(failure.message)});
 const current=draft||query.data?.settings,stale=!!current&&current.revision!==query.data?.settings?.revision;
 const choices=(providers.data?.providers||[]).filter(provider=>provider.protocol==='openai_chat');
 const selected=choices.find(provider=>provider.id===current?.provider_id);
 const selectedReady=providerReady(selected);
 function update(values:Partial<AutomationSettings>){if(current){setDraft({...current,...values});setDirty(true);setSaved(false);setError('');}}
 async function persist(){
  if(!current)return;setError('');setSaved(false);
  if(current.enabled&&!selectedReady){setError('请选择可用的文字 AI 服务后再启用自动分析。');return;}
  try{await save.mutateAsync(current);}catch(failure){setError(failure instanceof Error?failure.message:'自动分析设置保存失败');void query.refetch();}
 }
 const status=query.data?.status;
 return <section className="research-automation" aria-label="自动内容分析">
  <div className="ai-section-head"><div><Space wrap><h3>自动内容分析</h3><Tag color={status?.stale||status?.interrupted?'warning':status?.running?'processing':query.data?.settings?.enabled?'green':'default'}>{status?.stale?'运行状态待确认':status?.interrupted?'上次分析已中断':status?.running?'正在分析':query.data?.settings?.enabled?'已启用':'未启用'}</Tag></Space><p className="research-help">后台定时研究已采集资料；资料未变且报告仍有效时跳过调用，报告过期后重新研究。顾虑雷达统计犹豫和偏 No 市民的顾虑、他们对我们回复的反应和新闻，结果交给目标池、决策和写作步骤；排除本队、新闻和疑似 bot 账号。</p></div></div>
  {query.isPending&&<p role="status">正在读取自动分析设置…</p>}
  {query.isError&&<Alert type="error" showIcon title="自动分析设置读取失败" description={query.error.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>}
  {providers.isError&&<Alert type="error" title="AI 服务读取失败" description={providers.error.message}/>}
  {error&&<Alert type="error" title={error} showIcon/>}
  {saved&&<Alert type="success" title="自动分析设置已保存" showIcon/>}
  {current&&<>
   {stale&&<Alert type="warning" showIcon title="自动分析设置已在其他页面更新" action={<Button onClick={()=>{setDraft(query.data!.settings);setDirty(false);setError('');}}>载入最新设置</Button>}/>}
   <Form layout="vertical" disabled={save.isPending}>
    <div className="research-automation-grid">
     <Form.Item label="后台自动分析"><Switch aria-label="后台自动分析" checked={current.enabled} onChange={enabled=>update({enabled})}/></Form.Item>
     <Form.Item label="分析模式"><Select aria-label="自动分析模式" value={current.mode||'radar'} onChange={mode=>update({mode})} options={[{value:'radar',label:'顾虑雷达（推荐）'},{value:'popular',label:'热门帖子研究（旧）'}]}/></Form.Item>
     <Form.Item label="分析间隔 / 分钟"><InputNumber aria-label="自动分析间隔" min={15} max={1440} precision={0} value={current.interval_seconds/60} onChange={value=>{if(value!==null)update({interval_seconds:Number(value)*60});}}/></Form.Item>
     <Form.Item label="分析文字 AI 服务"><Select aria-label="自动分析文字 AI 服务" value={current.provider_id||undefined} placeholder="选择已配置的文字服务" onChange={provider_id=>update({provider_id})} options={choices.map(provider=>({value:provider.id,label:`${provider.name} · ${provider.model}${providerReady(provider)?'':'（不可用）'}`,disabled:!providerReady(provider)}))}/></Form.Item>
    </div>
    <div className="research-automation-reference"><Switch aria-label="自动供 Agent 参考" checked={current.auto_activate} onChange={auto_activate=>update({auto_activate})}/><div><strong>自动将合格报告供 Agent 参考</strong><p className="research-help">引用和来源校验通过后更新参考；关闭时只保存报告，由你手动选择。</p></div></div>
    <Collapse items={[{key:'scope',label:'研究范围与要求',children:<div className="stack">
     <div><p className="research-scope">自动分析范围：{scopeText(current.params)}</p><Button onClick={()=>update({params:scopeFrom(params)})}>使用当前页面的帖子范围</Button><p className="research-help">修改页面筛选不会改变后台任务；点击此按钮并保存后生效。</p></div>
     <Form.Item label="自动分析重点"><Input.TextArea aria-label="自动分析重点" value={current.instruction} maxLength={12000} autoSize={{minRows:2,maxRows:6}} onChange={event=>update({instruction:event.target.value})}/></Form.Item>
     <div className="research-automation-grid">
      <Form.Item label="最多分析帖子"><InputNumber aria-label="自动分析样本量" value={current.sample_size} min={20} max={200} precision={0} onChange={value=>{if(value!==null)update({sample_size:Number(value)});}}/></Form.Item>
      <Form.Item label="补充新闻资料"><Switch aria-label="自动分析补充新闻" checked={current.include_news} onChange={include_news=>update({include_news})}/></Form.Item>
     </div>
    </div>}]} />
    <div className="research-run"><Button type="primary" loading={save.isPending} disabled={!dirty||stale||query.isError||(current.enabled&&(!selectedReady||providers.isError))} onClick={()=>void persist()}>保存自动分析设置</Button>{dirty&&<span>有未保存的修改</span>}<Link to="/ai/settings">管理 AI 服务 ↗</Link></div>
    <p className="research-help">启用并保存后会在后台调用所选 AI，按服务商规则计费；暂停后不再安排新分析，可用“取消本轮分析”停止当前请求。</p>
   </Form>
   {status&&<div className="research-automation-status" aria-label="自动分析运行状态"><span>最近检查：{time(status.last_run_at)}</span><span>最近成功：{time(status.last_success_at)}</span><span>下次检查：{query.data?.settings.enabled?time(status.next_run_at):'已暂停'}</span>{status.last_result&&<span>{resultLabels[status.last_result.activated?'activated':status.last_result.outcome]||'检查已完成'}{status.last_result.activation_note&&` · ${status.last_result.activation_note}`}</span>}</div>}
   {status?.running&&<Alert type="info" showIcon role="status" title={status.cancel_requested?'正在取消本轮分析':`正在分析：${stageLabels[status.progress?.stage||'']||'等待模型完成'}`} action={<Button disabled={!status.run_id||status.cancel_requested} loading={cancel.isPending} onClick={()=>{if(status.run_id)cancel.mutate(status.run_id);}}>取消本轮分析</Button>} description={status.progress?`已用时 ${elapsedText(status.progress)}${status.progress.received_bytes?` · 已接收 ${Math.round(status.progress.received_bytes/1024)} KB`:''}；后台不限时等待思考完成，可关闭此页面。`:'后台不限时等待思考完成，可关闭此页面。'}/>}
   {status?.stale&&<Alert type="warning" showIcon role="status" title="运行心跳已中断" description={`最后记录用时 ${elapsedText({elapsed_seconds:status.progress?.elapsed_seconds??0})}；后台正在确认任务是否退出，确认前不会重复发送分析请求。`}/>}
   {status?.interrupted&&!status.stale&&<Alert type="warning" showIcon title="上次分析因服务中断而停止" description={status.last_error||'任务已结束，未生成新的报告。可以重新分析，按服务商规则计费。'} action={<Button loading={retry.isPending} disabled={!query.data?.settings.enabled||!status.interrupted_run_id} onClick={()=>{if(status.interrupted_run_id)retry.mutate(status.interrupted_run_id);}}>重新分析</Button>}/>}
   {!status?.running&&!status?.stale&&!status?.interrupted&&status?.last_error&&<Alert type="warning" showIcon title="上次自动分析未完成" description={status.last_error}/>}
  </>}
 </section>;
}

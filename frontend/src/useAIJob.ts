import {useEffect, useRef, useState} from 'react';
import {request, type Row} from './api';
type Job<T>={id:string;kind?:string;scope?:string;request_key?:string;state:'running'|'cancelling'|'completed'|'failed'|'cancelled';stage:string;workflow_step?:string;created_at?:string;elapsed_seconds?:number;received_bytes?:number;error?:string;result:T|null};
type Options<T>={path?:string;kind?:string;scopeKey?:string;onRecovered?:(data:T)=>void|Promise<void>};
type Active={id:string|null;token:string;key:string;cancelled:boolean;cancelSent:boolean;view:number;download?:AbortController;cancellation?:Promise<void>};
const labels:Record<string,string>={preparing:'整理资料',request:'发送请求',dns:'解析服务地址',connect:'连接服务',tls:'建立安全连接',send:'发送请求',headers:'等待模型回应',body:'接收回答',thinking:'模型正在思考',receiving:'正在接收回答',complete:'回答已完成'};
async function bounded<T=Row>(path:string,options:RequestInit={}) {
 const controller=new AbortController(),timer=window.setTimeout(()=>controller.abort(),10000);
 try{return await request<T>(path,{...options,signal:controller.signal});}
 finally{window.clearTimeout(timer);}
}
async function post<T>(path:string,payload:Row,token:string,headers:Record<string,string>={}) {
 return bounded<T>(path,{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':token,...headers},body:JSON.stringify(payload)});
}
async function cancelJob(current:Active):Promise<void>{
 if(!current.id||current.cancelSent)return;
 if(current.cancellation)return current.cancellation;
 const controller=new AbortController(),timer=window.setTimeout(()=>controller.abort(),10000);
 current.cancellation=(async()=>{
  try{
   const response=await fetch('/api/ai/jobs/'+current.id+'/cancel',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':current.token},body:'{}',signal:controller.signal});
   if(!response.ok){
    if(response.status===401)window.dispatchEvent(new Event('ctn:unauthorized'));
    const data=await response.json();throw new Error(data.error||data.detail||`请求失败 (${response.status})`);
   }
   // Cancellation only needs the acknowledgement. An already completed job
   // may include a large result, which the user has just asked us to discard.
   current.cancelSent=true;
   try{await response.body?.cancel();}catch{/* acknowledgement already received */}
  }finally{window.clearTimeout(timer);}
 })();
 try{await current.cancellation;}finally{current.cancellation=undefined;}
}
export function useAIJob<T>(options:Options<T>={}) {
 const path=options.path||'/ai/jobs',kind=options.kind||'analysis',scope=options.scopeKey||'';
 const [job,setJob]=useState<Job<T>|null>(null),[pending,setPending]=useState(false),[error,setError]=useState<Error|null>(null),[data,setData]=useState<T>(),[recoveredResult,setRecovered]=useState<T>(),[variables,setVariables]=useState<Row>(),[clock,setClock]=useState(Date.now());
 const active=useRef<Active|null>(null),reserved=useRef(false),view=useRef(0),mounted=useRef(false),onRecovered=useRef(options.onRecovered);
 onRecovered.current=options.onRecovered;
 const storageKey='ctn-ai-job:'+kind+':'+scope;
 const remember=(id:string|null)=>{try{if(id)sessionStorage.setItem(storageKey,id);else sessionStorage.removeItem(storageKey);}catch{/* optional private storage */}};
 function update(value:Job<T>,current:Active){if(mounted.current&&current.view===view.current){setJob(value);setClock(Date.now());}}
 async function readJob(identifier:string,current:Active){
  if(current.cancelled)throw new Error('AI 任务已取消');
  if(!mounted.current||current.view!==view.current)throw new Error('任务在后台继续运行，可重新打开查看');
  const controller=new AbortController();current.download=controller;
  try{return await request<Job<T>>('/ai/jobs/'+identifier,{signal:controller.signal});}
  finally{if(current.download===controller)current.download=undefined;}
 }
 async function follow(initial:Job<T>,current:Active):Promise<T>{
  let value=initial;update(value,current);
  while(value.state==='running'||value.state==='cancelling'||(value.state==='completed'&&value.result===undefined)){
   if(!mounted.current||current.view!==view.current)throw new Error('任务在后台继续运行，可重新打开查看');
   if(current.cancelled){
    try{await cancelJob(current);}catch{
     if(mounted.current&&current.view===view.current)setJob(previous=>previous?{...previous,stage:'取消尚未确认，正在重试'}:previous);
     await new Promise(resolve=>window.setTimeout(resolve,1000));continue;
    }
    throw new Error('AI 任务已取消');
   }
   await new Promise(resolve=>window.setTimeout(resolve,1000));
   if(!mounted.current||current.view!==view.current)throw new Error('任务在后台继续运行，可重新打开查看');
   if(current.cancelled)continue;
   try{value=await readJob(value.id,current);update(value,current);}
   catch(failure){
    if(current.cancelled)continue;
    if(mounted.current&&current.view===view.current)setJob(previous=>previous?{...previous,stage:'连接中断，后台继续运行，正在重连'}:previous);
    if(failure instanceof Error&&/不存在|已过期/.test(failure.message))throw failure;
   }
  }
  if(!mounted.current||current.view!==view.current)throw new Error('任务在后台继续运行，可重新打开查看');
  if(mounted.current&&current.view===view.current)remember(null);
  if(current.cancelled||value.state==='cancelled')throw new Error('AI 任务已取消');
  if(value.state!=='completed'||value.result===null)throw new Error(value.error||'AI 任务未完成');
  return value.result;
 }
 function finish(current:Active){
  if(active.current===current){active.current=null;reserved.current=false;if(mounted.current&&current.view===view.current)setPending(false);}
 }
 useEffect(()=>{
  mounted.current=true;const ownView=++view.current;
  active.current=null;reserved.current=false;setPending(false);setJob(null);setData(undefined);setVariables(undefined);setRecovered(undefined);setError(null);
  let disposed=false;
  void (async()=>{
   try{
    const listed=await bounded<{items:Job<T>[]}>('/ai/jobs?kind='+encodeURIComponent(kind)+'&scope='+encodeURIComponent(scope));
    if(disposed||reserved.current)return;
    let savedId:string|null=null;try{savedId=sessionStorage.getItem(storageKey);}catch{/* optional */}
    const rows=Array.isArray(listed.items)?listed.items:[];
    const selected=rows.find(item=>item.id===savedId)||rows.find(item=>item.state==='running'||item.state==='cancelling');
    if(!selected)return;
    const current:Active={id:selected.id,token:(await bounded('/bootstrap')).csrf_token,key:selected.request_key||'',cancelled:false,cancelSent:false,view:ownView};
    if(disposed||reserved.current)return;
    active.current=current;reserved.current=true;setPending(true);remember(selected.id);
    try{
     const result=await follow(selected,current);
     if(!disposed){setData(result);setRecovered(result);await onRecovered.current?.(result);remember(null);}
    }catch(failure){if(!disposed)setError(failure instanceof Error?failure:new Error('AI 任务未完成'));}
    finally{finish(current);}
   }catch{/* Failed status lookup never starts or cancels a model request. */}
  })();
  return()=>{disposed=true;mounted.current=false;view.current++;active.current?.download?.abort();};
 },[path,kind,scope]);
 // Only a running task needs a live elapsed clock. Keeping this timer alive
 // while idle also rerenders the entire page that owns the hook each second.
 useEffect(()=>{
  if(!pending)return;
  const timer=window.setInterval(()=>setClock(Date.now()),1000);
  return()=>window.clearInterval(timer);
 },[pending]);
 const mutateAsync=async(payload:Row):Promise<T>=>{
  if(reserved.current)throw new Error('已有 AI 任务正在运行，请等待完成或取消');
  reserved.current=true;
  const current:Active={id:null,token:'',key:crypto.randomUUID(),cancelled:false,cancelSent:false,view:view.current};
  active.current=current;setPending(true);setError(null);setVariables(payload);setRecovered(undefined);
  setJob({id:'',state:'running',stage:'提交任务',created_at:new Date().toISOString(),result:null});
  try{
   current.token=(await bounded('/bootstrap')).csrf_token;
   if(current.cancelled)throw new Error('AI 任务已取消');
   let started:Job<T>;
   try{started=await post<Job<T>>(path,payload,current.token,{'X-CTN-Job-Key':current.key,'X-CTN-Job-Scope':encodeURIComponent(scope)});}
   catch(failure){
    const listed=await bounded<{items:Job<T>[]}>('/ai/jobs?kind='+encodeURIComponent(kind));
    const found=listed.items?.find(item=>item.request_key===current.key);
    if(!found)throw failure;
    current.id=found.id;
    started=found;
   }
   current.id=started.id;remember(started.id);
   const result=await follow(started,current);
   if(mounted.current&&current.view===view.current){setData(result);remember(null);}
   return result;
  }catch(failure){
   const next=failure instanceof Error?failure:new Error('AI 任务未完成');
   if(mounted.current&&current.view===view.current)setError(next);
   throw next;
  }finally{finish(current);}
 };
 const cancel=async()=>{
  const current=active.current;if(!current)return;
  current.cancelled=true;current.download?.abort();setJob(previous=>previous?{...previous,stage:'正在取消'}:previous);
  await cancelJob(current);
 };
 const elapsedSeconds=job?.created_at?Math.max(0,(clock-Date.parse(job.created_at))/1000):job?.elapsed_seconds||0;
 const phase=job?.stage?labels[job.stage]||job.stage:'';
 return {mutateAsync,cancel,isPending:pending,isError:!!error,error,data,recoveredResult,variables,
  stage:job?.workflow_step&&job.workflow_step!==phase?job.workflow_step+' · '+phase:phase,
  elapsedSeconds,receivedBytes:job?.received_bytes||0,jobId:job?.id,
  reset:()=>{setData(undefined);setRecovered(undefined);setError(null);}};
}
